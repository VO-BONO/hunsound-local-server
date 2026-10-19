#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HUN STUDIO 로컬 처리 서버
=========================
HUNSOUND 웹앱(React 빌드본)의 무거운 오디오 처리를 담당하는 로컬 서버입니다.
웹앱은 http://127.0.0.1:8765/ 로 접속했을 때 /api/* 상대 경로로 이 서버를 호출합니다.

제공 기능:
  - GET  /api/health            : 서버 상태 (웹앱이 10초 타임아웃으로 호출)
  - POST /api/upload            : 음원 업로드 (multipart, 필드명 "file")
  - POST /api/mixing            : 멀티트랙 믹싱 (ffmpeg)
  - POST /api/master            : 마스터링 (ffmpeg loudnorm 2패스)
  - POST /api/stem              : AI 스템 분리 6종 (demucs htdemucs_6s)
  - POST /api/vocal_analysis    : 음정 분석 (librosa)
  - POST /api/vocaldyne         : 보컬 음정 수정 (미구현 — 명확한 에러 반환)
  - POST /api/export            : 포맷 변환 내보내기 (ffmpeg)
  - POST /api/enhance-export    : 오디오 향상 내보내기 (ffmpeg)
  - GET  /api/jobs               : 잡 목록
  - GET  /api/jobs/{id}          : 잡 상태 조회 (폴링용)
  - POST /api/jobs/{id}/library : 라이브러리 저장 표시
  - GET  /api/files/{id}/{파일명} : 결과 파일 다운로드 (?download=true 지원)
  - GET  /api/demo               : 데모 목록 (빈 배열)

설계 원칙:
  - 무거운 의존성(demucs, librosa, ffmpeg)이 없어도 서버는 정상 기동하고
    /api/health 가 응답합니다. 없는 기능의 잡은 error 상태 + 한글 메시지로 반환합니다.
  - 잡은 백그라운드 스레드에서 처리하고 progress(0~100)를 갱신합니다.
  - 모든 작업 파일은 ./data/ 폴더 아래에 저장됩니다.

실행:  python server.py        (또는 "HUN STUDIO 실행.command" 더블클릭)
"""

import json
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
import traceback
import wave
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

# ─────────────────────────────────────────────
# 1. 기본 설정
# ─────────────────────────────────────────────
HOST = "127.0.0.1"
PORT = 8765
VERSION = "1.0.0"

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = DATA_DIR / "uploads"      # 업로드 원본 보관
JOBS_DIR = DATA_DIR / "jobs"          # 잡별 결과물 보관
WEB_DIR = BASE_DIR / "web"            # 정적 웹 UI (index.html + assets/)

for _d in (DATA_DIR, UPLOAD_DIR, JOBS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# 웹앱이 기대하는 스템 6종 (htdemucs_6s 출력 순서)
STEM_NAMES = ["vocals", "drums", "bass", "other", "guitar", "piano"]
# 잡 종류별 기본 결과 파일명 (웹앱의 b0() 매핑과 동일)
KIND_DEFAULT_FILE = {
    "mixing": "mixing.wav",
    "master": "master.wav",
    "vocaldyne": "vocaldyne.wav",
    "stem": "original.wav",
    "upload": "original.wav",
    "export": "export.wav",
}


def _now_iso():
    """현재 시각 (ISO 8601, UTC). 웹앱이 started_at/created 로 읽습니다."""
    return datetime.now(timezone.utc).isoformat()


# ─────────────────────────────────────────────
# 2. 의존성 확인 (없어도 서버 기동에는 지장 없음)
# ─────────────────────────────────────────────
def has_ffmpeg():
    """ffmpeg 설치 여부."""
    return shutil.which("ffmpeg") is not None


def has_demucs():
    """demucs 패키지 설치 여부."""
    try:
        import demucs  # noqa: F401
        return True
    except ImportError:
        return False


def has_librosa():
    """librosa 패키지 설치 여부."""
    try:
        import librosa  # noqa: F401
        return True
    except ImportError:
        return False


def need_ffmpeg():
    """ffmpeg 필수 작업에서 호출. 없으면 한글 에러를 던집니다."""
    if not has_ffmpeg():
        raise RuntimeError(
            "ffmpeg이 설치되어 있지 않습니다. "
            "Mac: brew install ffmpeg / Windows: https://www.gyan.dev/ffmpeg/builds/"
        )


def need_demucs():
    """demucs 필수 작업에서 호출. 없으면 한글 에러를 던집니다."""
    if not has_demucs():
        raise RuntimeError(
            "demucs 패키지가 필요합니다. "
            "가상환경에서 pip install demucs 를 실행해주세요. "
            "(첫 실행 시 htdemucs_6s 모델을 자동 다운로드합니다)"
        )


# ─────────────────────────────────────────────
# 3. 잡 레지스트리 (인메모리 + 스레드 안전)
# ─────────────────────────────────────────────
_jobs = {}
_jobs_lock = threading.Lock()


def _new_job(kind, name, params):
    """새 잡을 만들고 queued 상태로 등록합니다."""
    jid = uuid4().hex[:12]
    job = {
        "id": jid,
        "kind": kind,
        "name": name or kind,
        "status": "queued",          # queued | processing | done | error
        "progress": 0,               # 0~100 (웹앱이 Math.round(progress)로 표시)
        "message": "대기 중",
        "result": None,
        "error": None,
        "created": _now_iso(),
        "started_at": _now_iso(),
        "source_id": (params or {}).get("source_id"),
        "library_saved_at": None,
        "library_deleted_at": None,
    }
    with _jobs_lock:
        _jobs[jid] = job
    (JOBS_DIR / jid).mkdir(parents=True, exist_ok=True)
    return job


def _get_job(jid):
    with _jobs_lock:
        return _jobs.get(jid)


def _list_jobs():
    with _jobs_lock:
        return list(_jobs.values())


def _public_job(job):
    """웹앱에 반환할 잡 dict (내부용 params 제외)."""
    j = dict(job)
    j.pop("_params", None)
    j.pop("_thread", None)
    return j


def _set_progress(jid, pct, message=""):
    with _jobs_lock:
        job = _jobs.get(jid)
        if job:
            job["progress"] = max(0, min(100, round(pct, 1)))
            if message:
                job["message"] = message


def _finish_job(jid, result):
    with _jobs_lock:
        job = _jobs.get(jid)
        if job:
            job["status"] = "done"
            job["progress"] = 100
            job["message"] = "완료"
            job["result"] = result
            # 결과 메타를 디스크에도 보관
            try:
                (JOBS_DIR / jid / "meta.json").write_text(
                    json.dumps(job, ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8",
                )
            except Exception:
                pass


def _fail_job(jid, message):
    with _jobs_lock:
        job = _jobs.get(jid)
        if job:
            job["status"] = "error"
            job["message"] = message
            job["error"] = message


def start_job(kind, name, params, worker):
    """잡을 등록하고 백그라운드 스레드로 worker를 실행합니다."""
    job = _new_job(kind, name, params)
    job["_params"] = params or {}

    def _runner():
        jid = job["id"]
        _set_progress(jid, 1, "처리 시작")
        try:
            with _jobs_lock:
                _jobs[jid]["status"] = "processing"
            result = worker(job, job["_params"],
                            lambda p, m="": _set_progress(jid, p, m))
            _finish_job(jid, result or {})
        except Exception as e:
            # 에러는 한글 메시지로 — 웹앱이 message 그대로 보여줍니다
            _fail_job(jid, str(e) or "알 수 없는 오류가 발생했습니다.")
            traceback.print_exc()

    t = threading.Thread(target=_runner, daemon=True)
    job["_thread"] = t
    t.start()
    return _public_job(job)


# ─────────────────────────────────────────────
# 4. 오디오 유틸 (표준 라이브러리만 사용)
# ─────────────────────────────────────────────
def _run(cmd, **kwargs):
    """서브프로세스 실행. 실패하면 stderr 포함 에러를 던집니다."""
    r = subprocess.run(cmd, capture_output=True, text=True, **kwargs)
    if r.returncode != 0:
        err = (r.stderr or "").strip().splitlines()
        tail = "\n".join(err[-5:]) if err else ""
        raise RuntimeError(f"명령 실행 실패 ({' '.join(cmd[:3])}...): {tail}")
    return r


def wav_info(path):
    """WAV 파일의 (재생시간초, 샘플레이트, 채널수)."""
    with wave.open(str(path), "rb") as w:
        frames = w.getnframes()
        sr = w.getframerate()
        ch = w.getnchannels()
        return (frames / sr if sr else 0.0), sr, ch


def ffprobe_duration(path):
    """ffmpeg 출력에서 재생시간 파싱 (wav가 아닐 때 사용)."""
    try:
        r = subprocess.run(["ffmpeg", "-i", str(path)],
                           capture_output=True, text=True)
        m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", r.stderr or "")
        if m:
            h, mi, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
            return h * 3600 + mi * 60 + s
    except Exception:
        pass
    return 0.0


def audio_info(path):
    """오디오 파일 기본 정보. (duration, sample_rate, channels)."""
    path = Path(path)
    try:
        if path.suffix.lower() == ".wav":
            return wav_info(path)
    except Exception:
        pass
    if has_ffmpeg():
        dur = ffprobe_duration(path)
        return dur, 44100, 2
    return 0.0, 44100, 2


def _to_mono_wav_8k(src, dst):
    """분석용으로 8kHz 모노 WAV 변환 (ffmpeg 필요)."""
    need_ffmpeg()
    _run(["ffmpeg", "-y", "-i", str(src), "-ac", "1", "-ar", "8000",
          "-f", "wav", str(dst)])


def compute_waveform(path, bars=160):
    """0~1 정규화된 파형 배열 (웹앱 파형 표시용).

    wav는 직접 읽고, 그 외 형식은 ffmpeg으로 변환 후 계산합니다.
    둘 다 안 되면 0으로 채운 배열을 반환합니다 (서버는 죽지 않음).
    """
    try:
        wav_path = Path(path)
        tmp = None
        if wav_path.suffix.lower() != ".wav":
            if not has_ffmpeg():
                return [0.0] * bars
            tmp = wav_path.parent / (wav_path.stem + "._wf8k.wav")
            _to_mono_wav_8k(wav_path, tmp)
            wav_path = tmp
        with wave.open(str(wav_path), "rb") as w:
            n_frames = w.getnframes()
            n_ch = w.getnchannels()
            sampwidth = w.getsampwidth()
            raw = w.readframes(n_frames)
        if tmp and tmp.exists():
            tmp.unlink()
        if not raw or n_frames == 0:
            return [0.0] * bars
        # 샘플 포맷별 언팩
        fmt = {1: "b", 2: "h", 4: "i"}.get(sampwidth)
        if not fmt:
            return [0.0] * bars
        count = len(raw) // sampwidth
        samples = struct.unpack(f"<{count}{fmt}", raw[:count * sampwidth])
        # 스테레오면 채널 평균
        if n_ch > 1:
            samples = [sum(samples[i:i + n_ch]) / n_ch
                       for i in range(0, len(samples) - n_ch + 1, n_ch)]
        peak = max([abs(s) for s in samples] or [1]) or 1
        per = max(1, len(samples) // bars)
        out = []
        for b in range(bars):
            chunk = samples[b * per:(b + 1) * per]
            out.append(round(max([abs(s) for s in chunk] or [0]) / peak, 4))
        return out
    except Exception:
        traceback.print_exc()
        return [0.0] * bars


def resolve_source(params):
    """요청 파라미터에서 원본 오디오 파일 경로를 찾습니다.

    file_id / source_id / upload_id / job_id / filename 등 다양한 키를 허용합니다.
    업로드 폴더와 잡 결과 폴더를 순서대로 뒤집니다.
    """
    if not params:
        raise RuntimeError("원본 음원을 찾을 수 없습니다. (파라미터 없음)")
    cands = []
    for key in ("file_id", "source_id", "upload_id", "job_id", "id"):
        v = params.get(key)
        if v:
            cands.append(str(v))
    fname = params.get("filename") or params.get("file")

    # 1) 업로드 폴더에서 찾기
    for cid in cands:
        udir = UPLOAD_DIR / cid
        if udir.is_dir():
            if fname and (udir / fname).exists():
                return udir / fname
            for p in sorted(udir.glob("original.*")):
                return p
            for p in sorted(udir.iterdir()):
                if p.is_file() and p.suffix.lower() in (
                        ".wav", ".mp3", ".m4a", ".flac", ".ogg", ".aiff", ".aif"):
                    return p
    # 2) 잡 결과 폴더에서 찾기
    for cid in cands:
        jdir = JOBS_DIR / cid
        if jdir.is_dir():
            if fname and (jdir / fname).exists():
                return jdir / fname
            for name in ("master.wav", "mixing.wav", "vocaldyne.wav",
                         "export.wav", "original.wav"):
                if (jdir / name).exists():
                    return jdir / name
            for p in sorted(jdir.glob("*.wav")):
                return p
    raise RuntimeError(
        "원본 음원 파일을 찾을 수 없습니다. "
        "먼저 음원을 업로드하거나, 해당 작업의 결과물이 있는지 확인해주세요."
    )

# ─────────────────────────────────────────────
# 5. 잡 워커들 (백그라운드 스레드에서 실행)
#    시그니처: worker(job, params, progress) -> result dict
#    progress(pct, message) 로 진행률을 갱신합니다.
# ─────────────────────────────────────────────
def _job_wav(job, name):
    """잡 결과 WAV 경로."""
    return JOBS_DIR / job["id"] / name


def worker_stem(job, p, progress):
    """AI 스템 분리 (demucs htdemucs_6s → 6종)."""
    need_demucs()
    progress(3, "원본 음원 확인 중")
    src = resolve_source(p)
    jdir = JOBS_DIR / job["id"]
    outdir = jdir / "demucs_out"
    outdir.mkdir(parents=True, exist_ok=True)

    progress(8, "AI 모델 로딩 중 (첫 실행은 다운로드로 오래 걸립니다)")
    # demucs 실행: -n htdemucs_6s (6스템: vocals/drums/bass/other/guitar/piano)
    cmd = [sys.executable, "-m", "demucs",
           "-n", "htdemucs_6s",
           "--out", str(outdir),
           str(src)]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    # demucs는 진행률을 기계적으로 주지 않으므로 단계별 메시지로 표시
    while proc.poll() is None:
        progress(30, "스템 분리 중... (수 분 소요될 수 있습니다)")
        time.sleep(5)
    if proc.returncode != 0:
        tail = (proc.stdout.read() or "")[-800:] if proc.stdout else ""
        raise RuntimeError(f"스템 분리 실패:\n{tail}")

    progress(88, "결과 정리 중")
    # demucs 출력: outdir/htdemucs_6s/<곡명>/{stem}.wav
    produced = sorted(outdir.rglob("*.wav"))
    if not produced:
        raise RuntimeError("스템 분리 결과가 생성되지 않았습니다.")
    stems = []
    for stem_name in STEM_NAMES:
        match = next((f for f in produced
                      if f.stem == stem_name), None)
        if match is None:
            continue
        dest = jdir / f"{stem_name}.wav"
        shutil.copy(match, dest)
        dur, sr, ch = audio_info(dest)
        stems.append({
            "name": stem_name,
            "file": dest.name,
            "duration": round(dur, 2),
            "sample_rate": sr,
            "waveform": compute_waveform(dest),
        })
    if not stems:
        raise RuntimeError("스템 파일을 찾지 못했습니다.")
    progress(100, "완료")
    dur, _, _ = audio_info(jdir / "vocals.wav")
    return {
        "stems": stems,
        "duration": round(dur, 2),
        "waveform": compute_waveform(jdir / "vocals.wav"),
        "model": "htdemucs_6s",
    }


def _track_filter(t, idx):
    """한 트랙에 적용할 ffmpeg 오디오 필터 문자열을 만듭니다.

    웹앱이 보내는 트랙 필드는 버전마다 다를 수 있어 .get() 기본값으로 방어합니다.
    """
    f = []
    # 음소거
    if t.get("mute"):
        return "volume=0"
    # 볼륨 (dB 또는 배율)
    gain_db = t.get("gain_db", t.get("gain", 0))
    try:
        gain_db = float(gain_db)
    except (TypeError, ValueError):
        gain_db = 0.0
    vol = t.get("volume", None)
    if vol is not None:
        try:
            f.append(f"volume={float(vol):.4f}")
        except (TypeError, ValueError):
            pass
    elif gain_db:
        f.append(f"volume={gain_db:.2f}dB")
    # 패닝 (-1=왼쪽 ~ 1=오른쪽)
    pan = t.get("pan", 0)
    try:
        pan = float(pan)
    except (TypeError, ValueError):
        pan = 0.0
    if pan:
        # pan=stereo|c0=..|c1=..
        left = min(1.0, 1.0 - pan)
        right = min(1.0, 1.0 + pan)
        f.append(f"pan=stereo|c0={left:.3f}*c0+{right:.3f}*c1"
                 f"|c1={left:.3f}*c0+{right:.3f}*c1")
    # 시작 오프셋 (초)
    offset = t.get("offset", 0) or 0
    try:
        if float(offset) > 0:
            f.append(f"adelay={int(float(offset)*1000)}|{int(float(offset)*1000)}")
    except (TypeError, ValueError):
        pass
    # 자르기
    ts = t.get("trim_start", 0) or 0
    te = t.get("trim_end", 0) or 0
    try:
        if float(ts) > 0 or float(te) > 0:
            if float(te) > 0:
                f.append(f"atrim=start={float(ts):.3f}:end={float(te):.3f}")
            else:
                f.append(f"atrim=start={float(ts):.3f}")
            f.append("asetpts=PTS-STARTPTS")
    except (TypeError, ValueError):
        pass
    # 페이드 인/아웃 (초)
    fi = t.get("fade_in", 0) or 0
    fo = t.get("fade_out", 0) or 0
    try:
        if float(fi) > 0:
            f.append(f"afade=t=in:st=0:d={float(fi):.3f}")
        if float(fo) > 0:
            # 전체 길이를 모르므로 곡선 페이드는 믹스 후 적용 권장 — 여기선 근사
            f.append(f"afade=t=out:st=0:d={float(fo):.3f}")
    except (TypeError, ValueError):
        pass
    return ",".join(f) if f else "anull"


def _atempo_chain(tempo):
    """atempo 필터 체인 (0.5~100 범위를 여러 개 이어붙임)."""
    tempo = float(tempo)
    if abs(tempo - 1.0) < 1e-6:
        return ""
    parts = []
    t = tempo
    while t > 2.0:
        parts.append("atempo=2.0")
        t /= 2.0
    while t < 0.5:
        parts.append("atempo=0.5")
        t /= 0.5
    parts.append(f"atempo={t:.4f}")
    return ",".join(parts)


def worker_mixing(job, p, progress):
    """멀티트랙 믹싱 (ffmpeg filter_complex)."""
    need_ffmpeg()
    progress(4, "트랙 확인 중")
    tracks = p.get("tracks") or []
    # 음소거가 아닌 트랙만 (솔로가 하나라도 있으면 솔로만)
    audible = [t for t in tracks if not t.get("mute")]
    solos = [t for t in audible if t.get("solo")]
    if solos:
        audible = solos
    if not audible:
        raise RuntimeError("믹싱할 트랙이 없습니다. 음원을 추가해주세요.")

    progress(8, "트랙 오디오 준비 중")
    jdir = JOBS_DIR / job["id"]
    inputs = []
    filters = []
    # 트랙 소스 파일 해결
    for i, t in enumerate(audible):
        src = None
        for key in ("file_id", "job_id", "upload_id", "source_id", "id"):
            if t.get(key):
                try:
                    src = resolve_source({key: t[key],
                                          "filename": t.get("filename") or t.get("file")})
                    break
                except RuntimeError:
                    continue
        if src is None and t.get("filename"):
            # 잡 폴더 내 파일명 직접 지정
            cand = jdir / t["filename"]
            if cand.exists():
                src = cand
        if src is None:
            # 마지막 수단: filename으로 전체 업로드 폴더 탐색
            fn = t.get("filename") or t.get("file") or ""
            if fn:
                hits = list(UPLOAD_DIR.rglob(fn)) + list(JOBS_DIR.rglob(fn))
                if hits:
                    src = hits[0]
        if src is None:
            raise RuntimeError(
                f"트랙 '{t.get('name', i+1)}'의 음원 파일을 찾을 수 없습니다.")
        # wav로 정규화 (캐시)
        norm = jdir / f"_track{i}.wav"
        if not norm.exists():
            _run(["ffmpeg", "-y", "-i", str(src),
                  "-ac", "2", "-ar", "44100", "-f", "wav", str(norm)])
        inputs += ["-i", str(norm)]
        tf = _track_filter(t, i)
        filters.append(f"[{i}:a]{tf}[t{i}]")

    progress(30, "믹싱 중")
    n = len(audible)
    mix_filter = "".join(f"[t{i}]" for i in range(n))
    mix_filter += f"amix=inputs={n}:duration=longest:dropout_transition=0:normalize=0[mix]"
    chain = ";".join(filters + [mix_filter])

    # 후처리: 템포 / 출력 게인
    post = []
    tempo = p.get("tempo", 1) or 1
    at = _atempo_chain(tempo)
    if at:
        post.append(at)
    out_gain = p.get("output_gain", 0) or 0
    try:
        if float(out_gain):
            post.append(f"volume={float(out_gain):.2f}dB")
    except (TypeError, ValueError):
        pass
    # 헤드룸 확보용 리미터 (클리핑 방지)
    headroom = p.get("headroom", -3)
    try:
        post.append(f"alimiter=limit={abs(float(headroom))/20:.4f}:level=disabled")
    except (TypeError, ValueError):
        pass
    if post:
        chain += ";[mix]" + ",".join(post) + "[out]"
        map_lbl = "[out]"
    else:
        map_lbl = "[mix]"

    # tail(여운) 처리: 체인 안에 apad 추가 (-af와 filter_complex 병용 불가)
    tail = p.get("tail", 2) or 0
    try:
        if float(tail) > 0:
            chain += f";{map_lbl}apad=whole_dur={float(tail):.1f}[padded]"
            map_lbl = "[padded]"
    except (TypeError, ValueError):
        pass

    out_path = _job_wav(job, "mixing.wav")
    cmd = ["ffmpeg", "-y"]
    cmd += inputs
    cmd += ["-filter_complex", chain, "-map", map_lbl,
            "-ac", "2", "-ar", "44100", "-f", "wav", str(out_path)]
    _run(cmd)

    progress(92, "결과 분석 중")
    dur, sr, ch = audio_info(out_path)
    progress(100, "완료")
    return {
        "file": out_path.name,
        "duration": round(dur, 2),
        "sample_rate": sr,
        "channels": ch,
        "waveform": compute_waveform(out_path),
        "lufs": None,
        "tracks": n,
    }


def _loudnorm_measure(src):
    """loudnorm 1패스: 측정값(JSON) 반환."""
    r = _run(["ffmpeg", "-i", str(src), "-af",
              "loudnorm=print_format=json", "-f", "null", "-"])
    m = re.search(r"\{[^}]*\"input_i\"[^}]*\}", r.stderr, re.S)
    if not m:
        raise RuntimeError("loudnorm 측정 실패")
    return json.loads(m.group(0))


def worker_master(job, p, progress):
    """마스터링: loudnorm 2패스로 목표 LUFS + 트루피크 제한."""
    need_ffmpeg()
    progress(4, "원본 확인 중")
    src = resolve_source(p)
    target_lufs = float(p.get("target_lufs", p.get("lufs", -14)) or -14)
    true_peak = float(p.get("true_peak", -1.5) or -1.5)
    lra = float(p.get("lra", 11) or 11)

    progress(15, "음압 측정 중 (1/2)")
    m = _loudnorm_measure(src)

    progress(45, "마스터링 적용 중 (2/2)")
    jdir = JOBS_DIR / job["id"]
    out_path = _job_wav(job, "master.wav")
    af = (f"loudnorm=I={target_lufs}:TP={true_peak}:LRA={lra}"
          f":measured_I={m['input_i']}:measured_TP={m['input_tp']}"
          f":measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}"
          f":offset={m['target_offset']}:linear=true")
    _run(["ffmpeg", "-y", "-i", str(src), "-af", af,
          "-ac", "2", "-ar", "44100", "-f", "wav", str(out_path)])

    progress(90, "결과 측정 중")
    m2 = _loudnorm_measure(out_path)
    dur, sr, ch = audio_info(out_path)
    progress(100, "완료")
    return {
        "file": out_path.name,
        "duration": round(dur, 2),
        "sample_rate": sr,
        "channels": ch,
        "waveform": compute_waveform(out_path),
        "lufs": round(float(m2["input_i"]), 2),
        "true_peak": round(float(m2["input_tp"]), 2),
        "lra": round(float(m2["input_lra"]), 2),
        "target_lufs": target_lufs,
    }


def worker_export(job, p, progress):
    """포맷 변환 내보내기. scope: full(단일) | stems(개별+zip)."""
    need_ffmpeg()
    progress(5, "원본 확인 중")
    fmt = (p.get("format") or "wav").lower().lstrip(".")
    sr = int(p.get("sample_rate") or 44100)
    bit = int(p.get("bit_depth") or 16)
    scope = p.get("scope") or "full"
    jdir = JOBS_DIR / job["id"]

    files = []
    if scope == "stems":
        # 소스 잡의 스템들을 각각 변환
        src_job = _get_job(str(p.get("source_id") or ""))
        stems = ((src_job or {}).get("result") or {}).get("stems") or []
        if not stems:
            raise RuntimeError("내보낼 스템이 없습니다.")
        for i, s in enumerate(stems):
            progress(5 + int(85 * i / max(1, len(stems))),
                     f"변환 중: {s['name']}")
            src = JOBS_DIR / src_job["id"] / s["file"]
            out = jdir / f"{s['name']}.{fmt}"
            _convert_audio(src, out, fmt, sr, bit)
            files.append(out.name)
        # zip 묶음
        import zipfile
        zpath = jdir / "stems.zip"
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as zf:
            for fn in files:
                zf.write(jdir / fn, fn)
        files.append(zpath.name)
    else:
        src = resolve_source(p)
        out = jdir / f"export.{fmt}"
        progress(30, "변환 중")
        _convert_audio(src, out, fmt, sr, bit)
        files = [out.name]

    progress(95, "결과 분석 중")
    main = jdir / files[0]
    dur, _, _ = audio_info(main)
    progress(100, "완료")
    return {
        "files": files,
        "file": files[0],
        "format": fmt,
        "duration": round(dur, 2),
        "waveform": compute_waveform(main),
    }


def _convert_audio(src, dst, fmt, sr=44100, bit=16):
    """ffmpeg 포맷 변환."""
    cmd = ["ffmpeg", "-y", "-i", str(src), "-ar", str(sr)]
    if fmt in ("wav",):
        cmd += ["-c:a", f"pcm_s{bit}le"]
    elif fmt == "flac":
        cmd += ["-c:a", "flac"]
    elif fmt in ("mp3",):
        cmd += ["-c:a", "libmp3lame", "-b:a", "320k"]
    elif fmt in ("m4a", "aac"):
        cmd += ["-c:a", "aac", "-b:a", "256k"]
    elif fmt == "ogg":
        cmd += ["-c:a", "libvorbis", "-q:a", "6"]
    else:
        raise RuntimeError(f"지원하지 않는 포맷입니다: {fmt}")
    cmd += [str(dst)]
    _run(cmd)


def worker_vocal_analysis(job, p, progress):
    """음정 분석: librosa piptrack으로 피치 커브 추출."""
    progress(5, "원본 확인 중")
    src = resolve_source(p)
    try:
        import librosa
        import numpy as np
    except ImportError:
        raise RuntimeError(
            "음정 분석에는 librosa가 필요합니다. "
            "pip install librosa 를 실행해주세요."
        )
    progress(20, "오디오 로딩 중")
    y, sr = librosa.load(str(src), sr=22050, mono=True)
    progress(45, "피치 추출 중")
    # 0.1초 단위 피치 커브
    hop = int(sr * 0.1)
    fmin, fmax = librosa.midi_to_hz(36), librosa.midi_to_hz(84)  # C2~C6
    pitches, mags = librosa.piptrack(y=y, sr=sr, hop_length=hop,
                                     fmin=fmin, fmax=fmax)
    curve = []
    for t in range(pitches.shape[1]):
        idx = int(mags[:, t].argmax())
        hz = float(pitches[idx, t])
        midi = float(librosa.hz_to_midi(hz)) if hz > 0 else 0.0
        curve.append(round(midi, 2))
    # 데이터가 너무 크면 다운샘플 (최대 600 포인트)
    if len(curve) > 600:
        step = len(curve) / 600
        curve = [curve[int(i * step)] for i in range(600)]
    progress(95, "결과 정리 중")
    dur = len(y) / sr
    progress(100, "완료")
    return {
        "pitch_curve": curve,
        "pitch_curve_sr": 10.0,
        "duration": round(dur, 2),
        "waveform": compute_waveform(src),
    }


def worker_vocaldyne(job, p, progress):
    """보컬 음정 수정 — 이 서버 버전에서는 미구현임을 명확히 알립니다."""
    raise RuntimeError(
        "보컬다인(음정 수정)은 이 로컬 서버 버전에서 아직 지원하지 않습니다. "
        "스템 분리로 보컬만 뽑은 뒤 외부 툴을 이용해주세요."
    )


def worker_enhance(job, p, progress):
    """오디오 향상 내보내기: 노이즈 게이트 + 다이내믹 정리 + loudnorm."""
    need_ffmpeg()
    progress(5, "원본 확인 중")
    src = resolve_source(p)
    jdir = JOBS_DIR / job["id"]
    out_path = _job_wav(job, "enhanced.wav")
    progress(30, "향상 처리 중")
    # afftdn(노이즈 감소) + dynaudnorm + loudnorm
    af = ("afftdn=nf=-25,"
          "dynaudnorm=f=150:g=7,"
          "loudnorm=I=-16:TP=-1.5:LRA=11")
    _run(["ffmpeg", "-y", "-i", str(src), "-af", af,
          "-ac", "2", "-ar", "44100", "-f", "wav", str(out_path)])
    progress(90, "결과 분석 중")
    dur, sr, ch = audio_info(out_path)
    progress(100, "완료")
    return {
        "file": out_path.name,
        "duration": round(dur, 2),
        "sample_rate": sr,
        "channels": ch,
        "waveform": compute_waveform(out_path),
    }

# ─────────────────────────────────────────────
# 6. FastAPI 앱 + 엔드포인트
# ─────────────────────────────────────────────
try:
    from fastapi import FastAPI, UploadFile, File, HTTPException
    from fastapi.responses import JSONResponse, FileResponse
    from fastapi.staticfiles import StaticFiles
    import uvicorn
    _HAS_WEB = True
except ImportError:
    _HAS_WEB = False


def _err(status_code, message):
    """웹앱 형식의 에러 응답 ({"detail": "..."})."""
    return JSONResponse(status_code=status_code, content={"detail": message})


if _HAS_WEB:
    app = FastAPI(title="HUN STUDIO 로컬 서버", version=VERSION,
                  docs_url=None, redoc_url=None)

    @app.get("/api/health")
    def api_health():
        """서버 상태. 웹앱이 10초 타임아웃으로 호출하며
        status=="ok" 와 audio_processing 값을 확인합니다."""
        ff = has_ffmpeg()
        return {
            "status": "ok",
            "audio_processing": ff,   # 핵심 처리(믹싱/마스터링/변환) 가능 여부
            "ffmpeg": ff,
            "demucs": has_demucs(),   # AI 스템 분리 가능 여부
            "librosa": has_librosa(),  # 음정 분석 가능 여부
            "version": VERSION,
        }

    @app.get("/api/demo")
    def api_demo():
        """데모 음원 목록 (없으면 빈 배열)."""
        return []

    @app.post("/api/upload")
    async def api_upload(file: UploadFile = File(...)):
        """음원 업로드. 200MB 제한. 파형/재생시간을 계산해 반환합니다."""
        if file.size and file.size > 200 * 1024 * 1024:
            return _err(413, "최대 200 MB까지 업로드할 수 있습니다.")
        uid = uuid4().hex[:12]
        udir = UPLOAD_DIR / uid
        udir.mkdir(parents=True, exist_ok=True)
        # 원본 파일명 보존 + 안전한 저장명
        orig_name = file.filename or "audio"
        ext = Path(orig_name).suffix.lower() or ".wav"
        if ext not in (".wav", ".mp3", ".m4a", ".flac", ".ogg",
                       ".aiff", ".aif", ".wma"):
            ext = ".wav"
        dest = udir / f"original{ext}"
        size = 0
        with open(dest, "wb") as f:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > 200 * 1024 * 1024:
                    f.close()
                    shutil.rmtree(udir, ignore_errors=True)
                    return _err(413, "최대 200 MB까지 업로드할 수 있습니다.")
                f.write(chunk)
        dur, sr, ch = audio_info(dest)
        meta = {
            "id": uid,
            "name": Path(orig_name).stem,
            "kind": "upload",
            "status": "done",
            "file": dest.name,
            "size": size,
            "duration": round(dur, 2),
            "sample_rate": sr,
            "channels": ch,
            "waveform": compute_waveform(dest),
            "created": _now_iso(),
        }
        (udir / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        return meta

    @app.post("/api/mixing")
    async def api_mixing(payload: dict):
        job = start_job("mixing", payload.get("name") or "믹싱",
                        payload, worker_mixing)
        return job

    @app.post("/api/master")
    async def api_master(payload: dict):
        job = start_job("master", payload.get("name") or "마스터링",
                        payload, worker_master)
        return job

    @app.post("/api/stem")
    async def api_stem(payload: dict):
        job = start_job("stem", payload.get("name") or "스템 분리",
                        payload, worker_stem)
        return job

    @app.post("/api/vocal_analysis")
    async def api_vocal_analysis(payload: dict):
        job = start_job("vocal_analysis",
                        payload.get("name") or "음정 분석",
                        payload, worker_vocal_analysis)
        return job

    @app.post("/api/vocaldyne")
    async def api_vocaldyne(payload: dict):
        job = start_job("vocaldyne",
                        payload.get("name") or "보컬다인",
                        payload, worker_vocaldyne)
        return job

    @app.post("/api/export")
    async def api_export(payload: dict):
        job = start_job("export", payload.get("name") or "내보내기",
                        payload, worker_export)
        return job

    @app.post("/api/enhance-export")
    async def api_enhance_export(file: UploadFile = File(...)):
        """오디오 향상용 파일 업로드 + 향상 잡 생성."""
        uid = uuid4().hex[:12]
        udir = UPLOAD_DIR / uid
        udir.mkdir(parents=True, exist_ok=True)
        ext = Path(file.filename or "audio").suffix.lower() or ".wav"
        dest = udir / f"original{ext}"
        with open(dest, "wb") as f:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)
        job = start_job("enhance",
                        Path(file.filename or "audio").stem + " (향상)",
                        {"file_id": uid}, worker_enhance)
        return job

    @app.get("/api/jobs")
    def api_jobs():
        """잡 목록 (최신순)."""
        js = [_public_job(j) for j in _list_jobs()]
        js.sort(key=lambda j: j.get("created", ""), reverse=True)
        return js

    @app.get("/api/jobs/{jid}")
    def api_job(jid: str):
        """잡 상태 조회 (웹앱 폴링용)."""
        job = _get_job(jid)
        if not job:
            return _err(404, "작업을 찾을 수 없습니다.")
        return _public_job(job)

    def _lib_flag(jid: str, field: str):
        job = _get_job(jid)
        if not job:
            return _err(404, "작업을 찾을 수 없습니다.")
        with _jobs_lock:
            job[field] = _now_iso()
        return {"ok": True, "job": _public_job(job)}

    @app.post("/api/jobs/{jid}/library")
    def api_library(jid: str):
        return _lib_flag(jid, "library_saved_at")

    @app.post("/api/jobs/{jid}/library-delete")
    def api_library_delete(jid: str):
        return _lib_flag(jid, "library_deleted_at")

    @app.post("/api/jobs/{jid}/library-restore")
    def api_library_restore(jid: str):
        job = _get_job(jid)
        if not job:
            return _err(404, "작업을 찾을 수 없습니다.")
        with _jobs_lock:
            job["library_deleted_at"] = None
        return {"ok": True, "job": _public_job(job)}

    @app.get("/api/files/{fid}/{filename}")
    def api_file(fid: str, filename: str, download: bool = False):
        """결과 파일 다운로드. ?download=true 면 첨부파일로 전송."""
        # 경로 조작 방지
        if ".." in filename or "/" in filename or "\\" in filename:
            return _err(400, "잘못된 파일명입니다.")
        for base in (JOBS_DIR / fid, UPLOAD_DIR / fid):
            p = base / filename
            if p.is_file():
                return FileResponse(
                    path=str(p),
                    filename=filename,
                    media_type="audio/wav" if filename.endswith(".wav")
                    else "application/octet-stream",
                    content_disposition_type=(
                        "attachment" if download else "inline"),
                )
        return _err(404, "파일을 찾을 수 없습니다.")

    # ── 정적 웹 UI 서빙 (web/index.html 있을 때만, API보다 나중에 마운트) ──
    _index = WEB_DIR / "index.html"
    if _index.exists():
        app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True),
                  name="web")
    else:
        @app.get("/")
        def _no_ui():
            return {
                "status": "ok",
                "message": "HUN STUDIO 로컬 서버가 실행 중입니다. "
                           "web/ 폴더에 index.html을 넣으면 웹 UI가 표시됩니다.",
                "api": "/api/health",
            }


# ─────────────────────────────────────────────
# 7. 실행
# ─────────────────────────────────────────────
def main():
    if not _HAS_WEB:
        print("=" * 60)
        print("[오류] fastapi / uvicorn이 설치되어 있지 않습니다.")
        print("다음 명령으로 설치해주세요:")
        print("  pip install -r requirements.txt")
        print("=" * 60)
        sys.exit(1)

    print("=" * 60)
    print(f" HUN STUDIO 로컬 서버 v{VERSION}")
    print(f" 주소: http://{HOST}:{PORT}/")
    print(f" 데이터 폴더: {DATA_DIR}")
    print(f" ffmpeg: {'있음' if has_ffmpeg() else '없음(믹싱·마스터링 불가)'}")
    print(f" demucs: {'있음' if has_demucs() else '없음(스템 분리 불가)'}")
    print(f" librosa: {'있음' if has_librosa() else '없음(음정 분석 불가)'}")
    print(" 종료: Ctrl+C")
    print("=" * 60)
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
