# actions.py
import os
import re
import mimetypes
import json
import sqlite3
from typing import Any, Text, Dict, List, Tuple
from datetime import datetime

import pytz
import requests
import pandas as pd

from rasa_sdk import Action, Tracker
from rasa_sdk.executor import CollectingDispatcher
from rasa_sdk.events import SlotSet, EventType
from rasa_sdk.types import DomainDict

# ---------- Gemini (파일 요약용 SDK) ----------
import google.generativeai as genai

# ---------- 실시간 파일 로깅 (패키지 임포트 경로) ----------
from actions.log_utils import ConversationLogger


# =========================
# 0) 설정
# =========================
GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "AIzaSyC0dAtVCMLn-CqwDYK8-mwnaIvZ4EDNpNs")

CHAT_MODEL_URL: str = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
)
FILE_MODEL_NAME: str = "models/gemini-1.5-pro"
genai.configure(api_key=GEMINI_API_KEY)
g_model = genai.GenerativeModel(FILE_MODEL_NAME)

KB_PATH = os.getenv("KB_PATH", "kb.txt")
KB_SEP = os.getenv("KB_SEP", "\t")

BASE_DIR_FROM_ENV = os.getenv("CHAT_LOG_DIR")
if BASE_DIR_FROM_ENV:
    CHAT_LOG_DIR = os.path.abspath(BASE_DIR_FROM_ENV)
else:
    CHAT_LOG_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "chat_logs"))
os.makedirs(CHAT_LOG_DIR, exist_ok=True)

JSONL_PATH              = os.path.join(CHAT_LOG_DIR, "history.jsonl")             # 종합 저장(기존)
JSONL_PATH_INTERNAL     = os.path.join(CHAT_LOG_DIR, "history_internal.jsonl")    # 내부 전용
JSONL_PATH_GEMINI       = os.path.join(CHAT_LOG_DIR, "history_gemini.jsonl")      # 외부(Gemini) 전용
SQLITE_PATH             = os.path.join(CHAT_LOG_DIR, "history.sqlite")

AUTO_SAVE_HISTORY = os.getenv("AUTO_SAVE_HISTORY", "true").lower() in ("1", "true", "yes")
SAVE_BACKEND = os.getenv("SAVE_BACKEND", "both").lower()  # both | jsonl | sqlite

print(f"[HISTORY] CHAT_LOG_DIR={CHAT_LOG_DIR}")
print(f"[HISTORY] JSONL_PATH(all)     ={JSONL_PATH}")
print(f"[HISTORY] JSONL_PATH(internal)={JSONL_PATH_INTERNAL}")
print(f"[HISTORY] JSONL_PATH(gemini)  ={JSONL_PATH_GEMINI}")
print(f"[HISTORY] SQLITE_PATH         ={SQLITE_PATH}")

# ▶ ▶ 실시간 이벤트 로거: 모드별 분리 저장
logger = ConversationLogger(base_dir=CHAT_LOG_DIR, split_by_mode=True)


# =========================
# 1) KB 캐시 (TXT/CSV/XLSX)
# =========================
class KBCache:
    def __init__(self, path: str):
        self.path = path
        self.mtime = 0.0
        self.topics: Dict[str, str] = {}
        self.synonyms: Dict[str, str] = {}
        self._load(force=True)

    def _load(self, force: bool = False):
        if not os.path.exists(self.path):
            if force:
                print(f"[KB] 파일이 없습니다: {self.path}")
            return

        cur = os.path.getmtime(self.path)
        if (cur == self.mtime) and not force:
            return

        ext = os.path.splitext(self.path)[1].lower()
        if ext in [".xlsx", ".xls"]:
            df = pd.read_excel(self.path, sheet_name="kb")
        elif ext == ".csv":
            df = pd.read_csv(self.path)
        else:
            df = pd.read_csv(self.path, sep=KB_SEP)

        cols_lower = {c.lower(): c for c in df.columns}

        def need(col: str) -> str:
            if col in cols_lower:
                return cols_lower[col]
            raise ValueError(f"[KB] '{col}' 컬럼이 없습니다. 현재 컬럼: {list(df.columns)}")

        tcol = need("topic")
        acol = need("answer")
        scol = cols_lower.get("synonyms")

        topics: Dict[str, str] = {}
        synonyms: Dict[str, str] = {}

        for _, row in df.iterrows():
            topic = "" if pd.isna(row[tcol]) else str(row[tcol]).strip()
            if not topic:
                continue
            answer = "" if pd.isna(row[acol]) else str(row[acol]).strip()
            topics[topic] = answer

            synonyms[topic.lower()] = topic
            if scol and not pd.isna(row[scol]):
                for phrase in [s.strip() for s in str(row[scol]).split(",") if str(s).strip()]:
                    synonyms[phrase.lower()] = topic

        self.topics, self.synonyms, self.mtime = topics, synonyms, cur
        print(f"[KB] 로드 완료: {self.path} (rows={len(self.topics)})")

    def maybe_reload(self): self._load()

    def find_topic(self, user_text: str) -> str:
        self.maybe_reload()
        text = (user_text or "").lower()
        keys = sorted(self.synonyms.keys(), key=len, reverse=True)
        for k in keys:
            if k and (k in text):
                return self.synonyms[k]
        return ""

    def get_answer(self, topic: str) -> str:
        return self.topics.get(topic, "")

KB = KBCache(KB_PATH)


# =========================
# 2) 유틸
# =========================
def now_in_seoul() -> str:
    seoul_time = datetime.now(pytz.timezone("Asia/Seoul"))
    return seoul_time.strftime("%Y년 %m월 %d일 %A %p %I시 %M분")

def is_time_question(msg: str) -> bool:
    msg = (msg or "").lower()
    triggers = ["현재 시간", "지금 몇시", "몇시", "오늘 날짜", "날짜", "오늘"]
    return any(t.lower() in msg for t in triggers)

URL_RE = re.compile(r"\b(https?://[^\s<>'\"]+)", re.IGNORECASE)
def clean_and_linkify(text: str) -> str:
    if not text: return text
    s = text.replace("https//", "https://").replace("http//", "http://")
    s = re.sub(r"<a[^>]*href=['\"]([^'\">]+)['\"][^>]*>.*?</a>", r"\1", s, flags=re.IGNORECASE | re.DOTALL)
    return URL_RE.sub(r"<a href='\1' target='_blank'>\1</a>", s)


# =========================
# 2-1) 히스토리 저장 유틸 (마스킹/JSONL/SQLite + 모드분리)
# =========================
EMAIL_RE = re.compile(r"([a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)")
PHONE_RE = re.compile(r"(\b01[016789]-?\d{3,4}-?\d{4}\b|\b\d{2,3}-\d{3,4}-\d{4}\b)")
RRN_RE = re.compile(r"\b\d{6}-\d{7}\b")

def mask_text(text: str) -> str:
    if not text: return text
    text = EMAIL_RE.sub("[EMAIL]", text)
    text = PHONE_RE.sub("[PHONE]", text)
    text = RRN_RE.sub("[RRN]", text)
    return text

def extract_history(tracker: Tracker) -> List[Dict[str, Any]]:
    history: List[Dict[str, Any]] = []
    for e in tracker.events:
        et = e.get("event")
        ts = e.get("timestamp")
        tstr = datetime.fromtimestamp(ts).isoformat() if ts else None
        if et == "user":
            history.append({
                "type": "user",
                "text": mask_text(e.get("text")),
                "intent": (e.get("parse_data") or {}).get("intent", {}).get("name"),
                "entities": (e.get("parse_data") or {}).get("entities", []),
                "time": tstr,
            })
        elif et == "bot":
            history.append({
                "type": "bot",
                "text": mask_text(e.get("text")),
                "time": tstr,
            })
    return history

def _split_history_by_mode(history: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    set_mode intent를 기준으로 내부/외부 구간을 나눕니다.
    반환: (internal_list, gemini_list, unknown_list)
    """
    current = "unknown"
    internal, gemini, unknown = [], [], []
    for m in history:
        # set_mode 명령이 있으면 모드 갱신
        if m.get("type") == "user" and m.get("intent") == "set_mode":
            # entities에서 mode 값 추출
            mode_val = None
            for ent in (m.get("entities") or []):
                if ent.get("entity") == "mode":
                    mode_val = (ent.get("value") or "").lower()
                    break
            if mode_val in ("internal", "내부"):
                current = "internal"
            elif mode_val in ("gemini", "외부"):
                current = "gemini"
            else:
                current = "unknown"
            continue

        target = internal if current=="internal" else gemini if current=="gemini" else unknown
        target.append(m)
    return internal, gemini, unknown

def get_session_id(tracker: Tracker) -> str:
    sid = tracker.sender_id or "unknown"
    first_user = next((e for e in tracker.events if e.get("event") == "user"), None)
    if first_user and first_user.get("timestamp"):
        return f"{sid}-{int(first_user['timestamp'])}"
    return sid

def _append_jsonl_to(path: str, record: Dict[str, Any]):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

def append_jsonl(sender_id: str, session_id: str, history: List[Dict[str, Any]]) -> None:
    record = {"sender_id": sender_id, "session_id": session_id, "saved_at": datetime.now().isoformat(), "history": history}
    _append_jsonl_to(JSONL_PATH, record)

def append_jsonl_split_by_mode(sender_id: str, session_id: str, history: List[Dict[str, Any]]) -> None:
    internal, gemini, unknown = _split_history_by_mode(history)
    ts = datetime.now().isoformat()
    if internal:
        _append_jsonl_to(JSONL_PATH_INTERNAL, {"sender_id": sender_id, "session_id": session_id, "saved_at": ts, "history": internal})
    if gemini:
        _append_jsonl_to(JSONL_PATH_GEMINI,   {"sender_id": sender_id, "session_id": session_id, "saved_at": ts, "history": gemini})

def init_sqlite():
    conn = sqlite3.connect(SQLITE_PATH)
    c = conn.cursor()
    c.execute("""
    CREATE TABLE IF NOT EXISTS conversations(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sender_id TEXT,
        session_id TEXT,
        saved_at TEXT
    )
    """)
    c.execute("""
    CREATE TABLE IF NOT EXISTS messages(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conv_id INTEGER,
        role TEXT,   -- 'user' or 'bot'
        text TEXT,
        intent TEXT,
        time TEXT,
        FOREIGN KEY(conv_id) REFERENCES conversations(id)
    )
    """)
    conn.commit()
    conn.close()

def insert_sqlite(sender_id: str, session_id: str, history: List[Dict[str, Any]]):
    init_sqlite()
    conn = sqlite3.connect(SQLITE_PATH)
    c = conn.cursor()
    saved_at = datetime.now().isoformat()
    c.execute("INSERT INTO conversations(sender_id, session_id, saved_at) VALUES(?,?,?)",
              (sender_id, session_id, saved_at))
    conv_id = c.lastrowid
    for m in history:
        c.execute(
            "INSERT INTO messages(conv_id, role, text, intent, time) VALUES(?,?,?,?,?)",
            (conv_id, m.get("type"), m.get("text"), m.get("intent"), m.get("time"))
        )
    conn.commit()
    conn.close()

def save_history_all(tracker: Tracker):
    """선택 백엔드에 따라 저장 (both | jsonl | sqlite) + 모드별 JSONL 추가"""
    sender_id = tracker.sender_id
    session_id = get_session_id(tracker)
    history = extract_history(tracker)

    if SAVE_BACKEND in ("both", "jsonl"):
        append_jsonl(sender_id, session_id, history)
        # ▶ 추가: 모드별 분리 저장
        append_jsonl_split_by_mode(sender_id, session_id, history)
    if SAVE_BACKEND in ("both", "sqlite"):
        insert_sqlite(sender_id, session_id, history)


# =========================
# 3) 내부/외부(제미나이) 액션
# =========================
class ActionSmartAnswer(Action):
    def name(self) -> Text: return "action_smart_answer"

    def run(self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[Text, Any]) -> List[Dict[Text, Any]]:
        mode = tracker.get_slot("mode")
        user_message = tracker.latest_message.get("text", "").strip()

        if user_message:
            try: logger.log(sender_id=tracker.sender_id, role="user", text=user_message, mode=mode, meta={"action": self.name()})
            except Exception as e: print(f"[LOGGER][user] {e}")

        if not mode:
            msg = "먼저 모드를 선택해 주세요. (내부/외부)"
            dispatcher.utter_message(text=msg)
            try: logger.log(sender_id=tracker.sender_id, role="bot", text=msg, mode=mode, meta={"reason":"no_mode"})
            except Exception as e: print(f"[LOGGER][bot] {e}")
            return []

        if mode == "internal":
            if is_time_question(user_message):
                msg = f"현재 한국 시간은 {now_in_seoul()}입니다. 😊"
                dispatcher.utter_message(text=msg)
                try: logger.log(sender_id=tracker.sender_id, role="bot", text=msg, mode=mode)
                except Exception as e: print(f"[LOGGER][bot] {e}")
                if AUTO_SAVE_HISTORY:
                    try: save_history_all(tracker)
                    except Exception as e: print(f"[HISTORY][AUTO] save error: {e}")
                return []

            topic = KBCache.find_topic.__get__(KB, KBCache)(user_message)
            if topic:
                ans = KB.get_answer(topic).strip()
                msg = clean_and_linkify(ans) if ans else "내부 지식에서 답변이 비어 있습니다. KB를 확인해 주세요."
                dispatcher.utter_message(text=msg)
                try: logger.log(sender_id=tracker.sender_id, role="bot", text=msg, mode=mode, meta={"topic": topic})
                except Exception as e: print(f"[LOGGER][bot] {e}")
                if AUTO_SAVE_HISTORY:
                    try: save_history_all(tracker)
                    except Exception as e: print(f"[HISTORY][AUTO] save error: {e}")
                return []

            msg = self._get_category_guide(user_message) if self._is_company_category_query(user_message) else "내부 지식에서는 해당 질문에 대한 답을 찾을 수 없어요."
            dispatcher.utter_message(text=msg)
            try: logger.log(sender_id=tracker.sender_id, role="bot", text=msg, mode=mode)
            except Exception as e: print(f"[LOGGER][bot] {e}")
            if AUTO_SAVE_HISTORY:
                try: save_history_all(tracker)
                except Exception as e: print(f"[HISTORY][AUTO] save error: {e}")
            return []

        elif mode == "gemini":
            self._call_gemini_api(dispatcher, tracker, user_message, mode)
            if AUTO_SAVE_HISTORY:
                try: save_history_all(tracker)
                except Exception as e: print(f"[HISTORY][AUTO] save error: {e}")
            return []

        else:
            msg = "⚠️ 모드를 인식하지 못했어요. 다시 시도해주세요."
            dispatcher.utter_message(text=msg)
            try: logger.log(sender_id=tracker.sender_id, role="bot", text=msg, mode=mode)
            except Exception as e: print(f"[LOGGER][bot] {e}")
            return []

    def _call_gemini_api(self, dispatcher: CollectingDispatcher, tracker: Tracker, message: str, mode: str | None):
        headers = {"Content-Type": "application/json"}
        prompt = (
            "너는 '엔지켐생명과학'의 사내 업무를 도와주는 친절한 AI 비서야. "
            "과도한 수식어 없이 간결하고 정확하게 답해줘.\n\n"
            f"질문: {message}"
        )
        data = {"contents": [{"parts": [{"text": prompt}]}]}
        try:
            r = requests.post(CHAT_MODEL_URL, headers=headers, json=data, timeout=30)
            if not r.ok:
                msg = f"Gemini API 오류: HTTP {r.status_code}\n{r.text[:800]}"
                dispatcher.utter_message(text=msg)
                try: logger.log(sender_id=tracker.sender_id, role="system", text=msg, mode=mode, meta={"status": r.status_code})
                except Exception as e: print(f"[LOGGER][system] {e}")
                return
            j = r.json()
            text = ""
            if j.get("candidates"):
                parts = j["candidates"][0].get("content", {}).get("parts", [])
                if parts and parts[0].get("text"):
                    text = parts[0]["text"]
            if not text:
                msg = "Gemini 응답이 비어 있습니다. 잠시 후 다시 시도해 주세요."
                dispatcher.utter_message(text=msg)
                try: logger.log(sender_id=tracker.sender_id, role="system", text=msg, mode=mode, meta={"empty":"candidate_text"})
                except Exception as e: print(f"[LOGGER][system] {e}")
                return
            msg = text.strip()
            dispatcher.utter_message(text=msg)
            try: logger.log(sender_id=tracker.sender_id, role="bot", text=msg, mode=mode, meta={"src":"gemini"})
            except Exception as e: print(f"[LOGGER][bot] {e}")
        except requests.exceptions.Timeout:
            msg = "Gemini 응답 지연(타임아웃)입니다. 잠시 후 다시 시도해 주세요."
            dispatcher.utter_message(text=msg)
            try: logger.log(sender_id=tracker.sender_id, role="system", text=msg, mode=mode, meta={"error":"timeout"})
            except Exception as e: print(f"[LOGGER][system] {e}")
        except Exception as e:
            msg = f"Gemini 호출 중 예외: {e}"
            dispatcher.utter_message(text=msg)
            try: logger.log(sender_id=tracker.sender_id, role="system", text=msg, mode=mode, meta={"error":"exception"})
            except Exception as le: print(f"[LOGGER][system] {le}")

    def _is_company_category_query(self, message: str) -> bool:
        msg = message or ""
        return any(w in msg for w in ["부서", "팀", "업무", "프로세스", "신청", "복리", "후생", "규정", "정책", "연락처"])

    def _get_category_guide(self, message: str) -> str:
        msg = message or ""
        if any(w in msg for w in ["부서", "팀", "조직"]):
            return "어떤 부서 정보가 궁금하신가요? (예: R&D전략개발실, 국내영업팀, IT팀 등)"
        elif any(w in msg for w in ["업무", "프로세스", "신청"]):
            return "어떤 업무 프로세스가 궁금하신가요? (예: 휴가신청, 출장신청 등)"
        elif any(w in msg for w in ["복리", "후생", "혜택"]):
            return "복리후생 정보 중 어떤 부분이 궁금하신가요? (예: 점심시간, 주차, 연차수당 등)"
        else:
            return "안녕하세요! 회사 정보를 도와드릴게요. '회사 주소', '휴가 신청 방법'과 같이 질문해주시면 답변해드릴 수 있습니다."


# =========================
# 4) 모드 설정 액션 (SILENT)
# =========================
class ActionSetMode(Action):
    def name(self) -> Text: return "action_set_mode"

    def run(
        self,
        dispatcher: CollectingDispatcher,
        tracker: Tracker,
        domain: Dict[Text, Any]
    ) -> List[Dict[Text, Any]]:
        # 슬롯에서 모드 추출 및 정규화
        raw_mode = tracker.get_slot("mode")
        mode_map = {"내부": "internal", "외부": "gemini", "Gemini": "gemini"}
        mode = mode_map.get(raw_mode, raw_mode)

        # ❗침묵 전환: 안내 멘트/로그/히스토리 저장 모두 하지 않음
        # dispatcher.utter_message(...) 사용 금지
        # logger.log(..., role="system"/"bot", text="...모드로 전환...") 금지
        # save_history_all(tracker) 호출 금지

        # 슬롯만 세팅하고 종료
        return [SlotSet("mode", mode)]


# =========================
# 5) 파일 요약 액션 (PDF/Excel/CSV)
# =========================
class ActionSummarizeFile(Action):
    def name(self) -> Text: return "action_summarize_file"

    def run(self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: DomainDict) -> List[Dict[Text, Any]]:
        file_path = tracker.get_slot("uploaded_file_path")
        file_mime = tracker.get_slot("uploaded_file_mime")

        try: logger.log(sender_id=tracker.sender_id, role="user", text=f"[file_uploaded] path={file_path} mime={file_mime}", mode=tracker.get_slot("mode"), meta={"action": self.name()})
        except Exception as e: print(f"[LOGGER][user] {e}")

        if not file_path:
            msg = "업로드된 파일이 없어요. 먼저 파일을 올려주세요."
            dispatcher.utter_message(text=msg)
            try: logger.log(sender_id=tracker.sender_id, role="bot", text=msg, mode=tracker.get_slot("mode"))
            except Exception as e: print(f"[LOGGER][bot] {e}")
            return []

        if not file_mime:
            guessed, _ = mimetypes.guess_type(file_path)
            file_mime = guessed or "application/octet-stream"

        try:
            if file_mime == "application/pdf":
                uploaded = genai.upload_file(path=file_path, mime_type="application/pdf")
                prompt = {
                    "role": "user",
                    "parts": [
                        {"file_data": {"file_uri": uploaded.uri, "mime_type": "application/pdf"}},
                        {"text": "이 문서의 핵심을 5~7개 불릿으로 요약하고, 액션아이템이 있으면 따로 정리해줘."},
                    ],
                }
                resp = g_model.generate_content(prompt)
                msg = clean_and_linkify((getattr(resp, "text", "") or "").strip())
                dispatcher.utter_message(text=msg)
                try: logger.log(sender_id=tracker.sender_id, role="bot", text=msg, mode=tracker.get_slot("mode"), meta={"file_mime": file_mime})
                except Exception as e: print(f"[LOGGER][bot] {e}")

            elif file_mime in ("application/vnd.ms-excel", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"):
                df = pd.read_excel(file_path)
                head = df.head(30).to_markdown(index=False)
                stats = df.describe(include="all").to_markdown()
                prompt = f"""
다음 표 데이터에서 핵심 인사이트/추세/이상치/추천 액션을 간결한 불릿으로 요약해줘.
[미리보기(최대 30행)]
{head}

[기본 통계]
{stats}
"""
                resp = g_model.generate_content(prompt)
                msg = clean_and_linkify((getattr(resp, "text", "") or "").strip())
                dispatcher.utter_message(text=msg)
                try: logger.log(sender_id=tracker.sender_id, role="bot", text=msg, mode=tracker.get_slot("mode"), meta={"file_mime": file_mime})
                except Exception as e: print(f"[LOGGER][bot] {e}")

            elif file_mime == "text/csv":
                df = pd.read_csv(file_path)
                head = df.head(30).to_markdown(index=False)
                stats = df.describe(include="all").to_markdown()
                prompt = f"""
CSV 데이터 요약: 핵심 지표/추세/이상치/권고사항을 불릿으로 정리해줘.
[미리보기]
{head}

[기본 통계]
{stats}
"""
                resp = g_model.generate_content(prompt)
                msg = clean_and_linkify((getattr(resp, "text", "") or "").strip())
                dispatcher.utter_message(text=msg)
                try: logger.log(sender_id=tracker.sender_id, role="bot", text=msg, mode=tracker.get_slot("mode"), meta={"file_mime": file_mime})
                except Exception as e: print(f"[LOGGER][bot] {e}")
            else:
                msg = f"현재 지원하지 않는 파일 형식입니다: {file_mime}. PDF/Excel/CSV를 올려주세요."
                dispatcher.utter_message(text=msg)
                try: logger.log(sender_id=tracker.sender_id, role="bot", text=msg, mode=tracker.get_slot("mode"))
                except Exception as e: print(f"[LOGGER][bot] {e}")

        except Exception as e:
            msg = f"요약 중 오류가 발생했습니다: {e}"
            dispatcher.utter_message(text=msg)
            try: logger.log(sender_id=tracker.sender_id, role="system", text=msg, mode=tracker.get_slot("mode"), meta={"error":"summarize_exception"})
            except Exception as le: print(f"[LOGGER][system] {le}")

        return []


# =========================
# 6) 스텁/히스토리 액션 (변경 없음, 저장 경로 안내만 유지)
# =========================
class ActionAnswerInternal(Action):
    def name(self) -> Text: return "action_answer_internal"
    def run(self, dispatcher, tracker, domain): dispatcher.utter_message(text="(안내) 내부 답변은 action_smart_answer에서 처리합니다."); return []

class ActionAnswerGemini(Action):
    def name(self) -> Text: return "action_answer_gemini"
    def run(self, dispatcher, tracker, domain): dispatcher.utter_message(text="(안내) Gemini 답변은 action_smart_answer에서 처리합니다."); return []

class ActionDispatchQuery(Action):
    def name(self) -> Text: return "action_dispatch_query"
    def run(self, dispatcher, tracker, domain): dispatcher.utter_message(text="(안내) 질의 분기는 action_smart_answer에서 처리합니다."); return []

class ActionSaveHistory(Action):
    def name(self) -> Text: return "action_save_history"
    def run(self, dispatcher, tracker, domain) -> List[EventType]:
        try:
            save_history_all(tracker)
            msg = (
                "대화 히스토리를 저장했습니다.\n"
                f"- 종합 JSONL: {JSONL_PATH}\n"
                f"- 내부 JSONL: {JSONL_PATH_INTERNAL}\n"
                f"- 외부 JSONL: {JSONL_PATH_GEMINI}\n"
                f"- SQLite    : {SQLITE_PATH}"
            )
            dispatcher.utter_message(text=msg)
        except Exception as e:
            dispatcher.utter_message(text=f"히스토리 저장 중 오류가 발생했습니다: {e}")
        return []
