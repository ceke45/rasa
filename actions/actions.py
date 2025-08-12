# actions.py
import os
import time
import mimetypes
from typing import Any, Text, Dict, List
from datetime import datetime

import pytz
import requests
import pandas as pd

from rasa_sdk import Action, Tracker
from rasa_sdk.executor import CollectingDispatcher
from rasa_sdk.events import SlotSet
from rasa_sdk.types import DomainDict

# Gemini SDK (파일 요약용)
import google.generativeai as genai


# ============================================================================
# 0) 환경/상수
# ============================================================================
# ▶ 필요 패키지: pip install rasa-sdk google-generativeai pandas openpyxl
GEMINI_API_KEY: str = "AIzaSyC0dAtVCMLn-CqwDYK8-mwnaIvZ4EDNpNs"  # 하드코딩 버전
CHAT_MODEL_URL: str = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
)
FILE_MODEL_NAME: str = "models/gemini-1.5-pro"  # 무료키/권한 문제 있으면 flash로 바꿔도 됨
genai.configure(api_key=GEMINI_API_KEY)
g_model = genai.GenerativeModel(FILE_MODEL_NAME)

# KB 파일 경로(확장자에 따라 자동 처리). 기본값은 탭구분 텍스트.
KB_PATH = os.getenv("KB_PATH", "kb.txt")  # 예: "kb.xlsx" / "kb.csv" / "kb.txt"
KB_SEP = os.getenv("KB_SEP", "\t")        # txt/csv일 때 컬럼 구분자(기본: 탭)


# ============================================================================
# 1) KB 캐시 (TXT/CSV/XLSX -> 메모리 로드)
#    - 필수 컬럼: topic, answer
#    - 선택 컬럼: synonyms (쉼표 구분 동의어들)
# ============================================================================
class KBCache:
    def __init__(self, path: str):
        self.path = path
        self.mtime = 0.0
        self.topics: Dict[str, str] = {}     # {topic: answer}
        self.synonyms: Dict[str, str] = {}   # {phrase_lower: topic}
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
            # Excel: 시트명 'kb' 사용 가정
            df = pd.read_excel(self.path, sheet_name="kb")
        elif ext == ".csv":
            df = pd.read_csv(self.path)
        else:
            # txt 등: 기본은 탭 구분
            df = pd.read_csv(self.path, sep=KB_SEP)

        # 컬럼 이름 케이스 섞여도 인식되게
        cols_lower = {c.lower(): c for c in df.columns}

        def need(col: str) -> str:
            if col in cols_lower:
                return cols_lower[col]
            raise ValueError(f"[KB] '{col}' 컬럼이 없습니다. 현재 컬럼: {list(df.columns)}")

        tcol = need("topic")
        acol = need("answer")
        scol = cols_lower.get("synonyms", None)  # 선택

        topics: Dict[str, str] = {}
        synonyms: Dict[str, str] = {}

        for _, row in df.iterrows():
            topic = "" if pd.isna(row[tcol]) else str(row[tcol]).strip()
            if not topic:
                continue
            answer = "" if pd.isna(row[acol]) else str(row[acol]).strip()
            topics[topic] = answer

            # topic 자체도 키워드로
            synonyms[topic.lower()] = topic

            # 동의어(쉼표 구분)
            if scol and not pd.isna(row[scol]):
                syns = [s.strip() for s in str(row[scol]).split(",") if str(s).strip()]
                for phrase in syns:
                    synonyms[phrase.lower()] = topic

        self.topics = topics
        self.synonyms = synonyms
        self.mtime = cur
        print(f"[KB] 로드 완료: {self.path} (rows={len(self.topics)})")

    def maybe_reload(self):
        self._load()

    def find_topic(self, user_text: str) -> str:
        """동의어/키워드 부분일치로 topic 찾기 (긴 키워드 우선)"""
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


# ============================================================================
# 2) 유틸
# ============================================================================
def now_in_seoul() -> str:
    seoul_time = datetime.now(pytz.timezone("Asia/Seoul"))
    return seoul_time.strftime("%Y년 %m월 %d일 %A %p %I시 %M분")


def is_time_question(msg: str) -> bool:
    msg = (msg or "").lower()
    triggers = ["현재 시간", "지금 몇시", "몇시", "오늘 날짜", "날짜", "오늘"]
    return any(t.lower() in msg for t in triggers)


# ============================================================================
# 3) 내부/외부(제미나이) 응답 액션
# ============================================================================
class ActionSmartAnswer(Action):
    def name(self) -> Text:
        return "action_smart_answer"

    def run(
        self,
        dispatcher: CollectingDispatcher,
        tracker: Tracker,
        domain: Dict[Text, Any],
    ) -> List[Dict[Text, Any]]:
        mode = tracker.get_slot("mode")
        user_message = tracker.latest_message.get("text", "").strip()

        if not mode:
            dispatcher.utter_message(text="먼저 모드를 선택해 주세요. (내부/외부)")
            return []

        if mode == "internal":
            if is_time_question(user_message):
                dispatcher.utter_message(text=f"현재 한국 시간은 {now_in_seoul()}입니다. 😊")
                return []

            topic = KB.find_topic(user_message)
            if topic:
                ans = KB.get_answer(topic).strip()
                if ans:
                    dispatcher.utter_message(text=ans)
                else:
                    dispatcher.utter_message(text="내부 지식에서 답변이 비어 있습니다. KB를 확인해 주세요.")
                return []

            if self._is_company_category_query(user_message):
                dispatcher.utter_message(text=self._get_category_guide(user_message))
            else:
                dispatcher.utter_message(text="내부 지식에서는 해당 질문에 대한 답을 찾을 수 없어요.")
            return []

        elif mode == "gemini":
            self._call_gemini_api(dispatcher, user_message)
            return []

        else:
            dispatcher.utter_message(text="⚠️ 모드를 인식하지 못했어요. 다시 시도해주세요.")
            return []

    # ---- 외부(Gemini) 호출 (채팅/질의응답용: REST) ----
    def _call_gemini_api(self, dispatcher: CollectingDispatcher, message: str):
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
                dispatcher.utter_message(text=f"Gemini API 오류: HTTP {r.status_code}\n{r.text[:800]}")
                return
            j = r.json()
            text = ""
            if j.get("candidates"):
                parts = j["candidates"][0].get("content", {}).get("parts", [])
                if parts and parts[0].get("text"):
                    text = parts[0]["text"]
            if not text:
                dispatcher.utter_message(text="Gemini 응답이 비어 있습니다. 잠시 후 다시 시도해 주세요.")
                return
            dispatcher.utter_message(text=text.strip())
        except requests.exceptions.Timeout:
            dispatcher.utter_message(text="Gemini 응답 지연(타임아웃)입니다. 잠시 후 다시 시도해 주세요.")
        except Exception as e:
            dispatcher.utter_message(text=f"Gemini 호출 중 예외: {e}")

    # ---- 회사 카테고리형 질의 탐지/가이드(선택) ----
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


# ============================================================================
# 4) 모드 설정
# ============================================================================
class ActionSetMode(Action):
    def name(self) -> Text:
        return "action_set_mode"

    def run(
        self,
        dispatcher: CollectingDispatcher,
        tracker: Tracker,
        domain: Dict[Text, Any],
    ) -> List[Dict[Text, Any]]:
        raw_mode = tracker.get_slot("mode")
        mode_map = {"내부": "internal", "외부": "gemini", "Gemini": "gemini"}
        mode = mode_map.get(raw_mode, raw_mode)

        if mode == "internal":
            dispatcher.utter_message(response="utter_mode_set_internal")
        elif mode == "gemini":
            dispatcher.utter_message(response="utter_mode_set_gemini")
        else:
            dispatcher.utter_message(text="⚠️ 모드를 인식하지 못했어요. 다시 시도해주세요.")

        return [SlotSet("mode", mode)]


# ============================================================================
# 5) 파일 요약 액션 (PDF/Excel/CSV)
# ============================================================================
class ActionSummarizeFile(Action):
    def name(self) -> Text:
        return "action_summarize_file"

    def run(
        self,
        dispatcher: CollectingDispatcher,
        tracker: Tracker,
        domain: DomainDict,
    ) -> List[Dict[Text, Any]]:

        file_path = tracker.get_slot("uploaded_file_path")
        file_mime = tracker.get_slot("uploaded_file_mime")

        if not file_path:
            dispatcher.utter_message(text="업로드된 파일이 없어요. 먼저 파일을 올려주세요.")
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
                dispatcher.utter_message(text=(getattr(resp, "text", "") or "").strip())

            elif file_mime in (
                "application/vnd.ms-excel",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ):
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
                dispatcher.utter_message(text=(getattr(resp, "text", "") or "").strip())

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
                dispatcher.utter_message(text=(getattr(resp, "text", "") or "").strip())
            else:
                dispatcher.utter_message(text=f"현재 지원하지 않는 파일 형식입니다: {file_mime}. PDF/Excel/CSV를 올려주세요.")

        except Exception as e:
            dispatcher.utter_message(text=f"요약 중 오류가 발생했습니다: {e}")

        return []


# ============================================================================
# 6) 스텁 액션 (도메인 등록 대응)
# ============================================================================
class ActionAnswerInternal(Action):
    def name(self) -> Text:
        return "action_answer_internal"

    def run(self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[Text, Any]) -> List[Dict[Text, Any]]:
        dispatcher.utter_message(text="(안내) 내부 답변은 action_smart_answer에서 처리합니다.")
        return []


class ActionAnswerGemini(Action):
    def name(self) -> Text:
        return "action_answer_gemini"

    def run(self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[Text, Any]) -> List[Dict[Text, Any]]:
        dispatcher.utter_message(text="(안내) Gemini 답변은 action_smart_answer에서 처리합니다.")
        return []


class ActionDispatchQuery(Action):
    def name(self) -> Text:
        return "action_dispatch_query"

    def run(self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[Text, Any]) -> List[Dict[Text, Any]]:
        dispatcher.utter_message(text="(안내) 질의 분기는 action_smart_answer에서 처리합니다.")
        return []
