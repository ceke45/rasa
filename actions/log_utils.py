# log_utils.py
import os, json, datetime, threading, re
from pathlib import Path

_LOCK = threading.Lock()

# KST 타임존 (UTC+9)
KST_TZ = datetime.timezone(datetime.timedelta(hours=9))

def _today_str_kst() -> str:
    return datetime.datetime.now(KST_TZ).strftime("%Y-%m-%d")

def _ensure_dir(p: str | Path):
    Path(p).mkdir(parents=True, exist_ok=True)

# 모드 전환 패턴(안전장치)
_MODE_RE = re.compile(
    r'(모드\s*로\s*전환했|모드\s*전환|\[mode[_\s-]*change\]|switched\s+to.*mode|mode\s+changed)',
    re.IGNORECASE
)

class ConversationLogger:
    def __init__(
        self,
        base_dir: str | None = None,
        fixed_path: str | None = None,
        split_by_mode: bool = False,
        filename_template: str = "chat{mode_suffix}_{date}.jsonl",
    ):
        """
        - base_dir: 기본 로그 디렉터리
        - fixed_path: 이 파일에만 계속 쓰기 (우선순위 최상)
        - split_by_mode: True면 모드별 파일 분리 저장 (chat_internal_YYYY-MM-DD.jsonl 등)
        - filename_template: 파일명 템플릿. {date}, {mode_suffix} 사용 가능
        """
        self.base_dir = os.path.abspath(base_dir or os.getenv("CHAT_LOG_DIR", "./logs"))
        _ensure_dir(self.base_dir)

        self._fixed = False
        if fixed_path:
            self.path = os.path.abspath(fixed_path)
            _ensure_dir(Path(self.path).parent)
            self._fixed = True
        else:
            self.path = os.path.join(
                self.base_dir,
                filename_template.format(mode_suffix="", date=_today_str_kst())
            )

        self.split_by_mode = split_by_mode
        self.filename_template = filename_template

        print(f"[ConversationLogger] base_dir={self.base_dir} path={self.path} fixed={self._fixed} split_by_mode={self.split_by_mode}")

    def _path_for(self, mode: str | None) -> str:
        # 날짜 회전은 KST 기준
        date = _today_str_kst()
        mode_key = (mode or "unknown").lower()
        if mode_key in ("internal", "inside", "내부", "inhouse"):
            mode_key = "internal"
        elif mode_key in ("gemini", "외부", "external"):
            mode_key = "gemini"
        else:
            mode_key = "unknown"

        if self._fixed:
            return self.path

        if self.split_by_mode:
            fn = self.filename_template.format(mode_suffix=f"_{mode_key}", date=date)
        else:
            fn = self.filename_template.format(mode_suffix="", date=date)
        return os.path.join(self.base_dir, fn)

    def _is_mode_change(self, role: str, text: str | None, meta: dict | None) -> bool:
        # system 이거나, meta.action == action_set_mode 이거나, 텍스트가 모드전환 패턴이면 제외
        if (role or "").lower() == "system":
            return True
        m = meta or {}
        if isinstance(m, dict) and m.get("action") == "action_set_mode":
            return True
        return bool(_MODE_RE.search(text or ""))

    def log(self, *, sender_id: str, role: str, text: str, mode: str | None = None, meta: dict | None = None):
        """
        role: 'user' | 'bot' | 'system'
        - KST 전용 타임스탬프 저장 (UTC 저장 안 함)
        """
        # 모드전환/시스템 로그는 스킵(안전)
        if self._is_mode_change(role, text, meta):
            return

        path = self._path_for(mode)
        _ensure_dir(Path(path).parent)

        now = datetime.datetime.now(KST_TZ)
        entry = {
            "ts_kst": now.isoformat(timespec="seconds"),            # 예: 2025-08-14T17:12:34+09:00
            "ts_kst_human": now.strftime("%Y-%m-%d (%a) %H:%M:%S"), # 예: 2025-08-14 (Thu) 17:12:34
            "sender_id": sender_id,
            "role": role,
            "text": text,
            "mode": mode,
            "meta": meta or {},
        }

        line = json.dumps(entry, ensure_ascii=False)
        with _LOCK:
            try:
                with open(path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except Exception as e:
                print(f"[ConversationLogger][ERROR] write failed: {e} (path={path})")
