
import requests
from typing import Any, Text, Dict, List
from datetime import datetime
import pytz
from rasa_sdk import Action, Tracker
from rasa_sdk.executor import CollectingDispatcher
from rasa_sdk.events import SlotSet

# ----------------------------------------------------------------------
# ★ 1. 회사 내부 지식 베이스
# ----------------------------------------------------------------------
COMPANY_KB = {
    "회사이름": "저희 회사는 '엔지켐생명과학' 입니다.",
    "주소": "저희 대표 회사주소는 서울 서초구 강남대로 27, at센터 10층,14층에 있습니다.",
    "대표": "저희 회사 회장님 성함은 '손기영' 입니다.",
    "설립일": "저희 회사는 1997년 07월에 설립되었습니다.",
    "비전": "저희 회사의 비전은 '세계 1등 바이오·제약 챔피언, 건강장수 130, 엔지켐생명과학 3.0, 아름다운 삶 3.0' 입니다.",
    "R&D전략개발실": "R&D전략개발실은 신약 개발과 임상시험을 담당하며, 14층에 위치해 있습니다. 팀장은 김정석 이사입니다.",
    "국내영업팀": "국내영업팀은 제품 판매와 고객 관리를 담당하며, 14층에 위치해 있습니다. 팀장은 윤두환 차장입니다.",
    "인사팀": "인사팀은 채용, 교육, 복리후생을 담당하며, 10층에 위치해 있습니다. 팀장은 김성국 이사입니다.",
    "회계·세무팀": "회계·세무팀은 회계, 세무, 자금 관리를 담당하며, 10층에 위치해 있습니다. 팀장은 박성법 차장입니다.",
    "IT팀": "IT팀은 시스템 개발과 유지보수를 담당하며, 14층에 위치해 있습니다. 팀장은 장우혁 차장입니다.",
    "자금팀": "자금팀은 결제관련 업무 및 법인카드 관리를 담당하며,10층에 위치해 있습니다. 팀장은 이은옥 차장입니다.",
    "경영기획팀": "경영기획팀은 결제관련 내부회계 관련 업무를 담당하며,10층에 위치해 있습니다. 팀장은 이경석 차장입니다.",
    "글로벌건강기능식품팀": "글로벌건강기능식품팀은 건강기능식품 판매 및 관리 업무를 담당하며,10층에 위치해 있습니다. 팀장은 홍석민 부장입니다.",
    "휴가신청": "휴가신청은 인사시스템에서 신청하시면 됩니다. 연차는 15일, 반차는 0.5일로 계산됩니다.",
    "출장신청": "출장신청은 그룹웨어 전자결제를 통해 미리 신청서를 제출하고 승인을 받으셔야 합니다.",
    "구매신청": "구매신청은 전자결제 기안서를 통해서 진행하며, 사무용품인 경우 총무IT팀이 담당하고 있습니다.",
    "회의실예약": "회의실 예약은 그룹웨어의 '예약'을 통해서 진행할 수 있으며, 10층과 14층에 회의실이 있습니다.",
    "점심시간": "점심시간은 11시30분부터 12시30분, 12시30분부터 1시30분까지입니다. 지하1층 AT뷔페, 더온담, 영등포구석집, 싸다김밥에서 식사 가능합니다.",
    "퇴근시간": "퇴근시간은 오후 6시입니다. 퇴근 시 지문 등록을 꼭 하시길 바랍니다.",
    "주차": "주차 지원은 별도로 없으나, 원하실 경우 AT센터 지하 혹은 근처 공영주차장에 주차 가능합니다.",
    "연차수당": "연차를 다 소진하지 못할 경우 연차 수당이 지급됩니다.",
    "IT지원": "IT지원팀 연락처는 02-6213-7184입니다. 시스템 문제 시 언제든 연락주세요.",
    "보안팀": "보안팀의 공식적인 연락처는 사내 인트라넷을 확인해주시기 바랍니다. 출입증 분실 시 즉시 연락해야 합니다.",
    "복장규정": "무난한 캐주얼 복장을 선호합니다.",
    "출근시간": "출근시간은 오전 9시이며, 10분 전까지 도착하여 업무 준비를 권장드립니다.",
    "보안규정": "회사 내에서는 보안상 개인 PC에 파일 저장이 어렵습니다(TXT, 이미지 제외). 문서중앙화를 통해 작성 바랍니다."
}

# ----------------------------------------------------------------------
# ★ 2. Gemini API 설정
# ----------------------------------------------------------------------
GEMINI_API_KEY = "AIzaSyC0dAtVCMLn-CqwDYK8-mwnaIvZ4EDNpNs"
API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"


class ActionSmartAnswer(Action):
    def name(self) -> Text:
        return "action_smart_answer"

    def run(self, dispatcher: CollectingDispatcher,
            tracker: Tracker,
            domain: Dict[Text, Any]) -> List[Dict[Text, Any]]:
        mode = tracker.get_slot("mode")
        user_message = tracker.latest_message.get('text', '').strip()
        if not mode:
            dispatcher.utter_message(text="먼저 모드를 선택해 주세요. (내부/외부)")
            return []
        if mode == "internal":
            topic = self._find_topic_by_keywords(user_message.lower())
            if topic == "현재시간":
                dispatcher.utter_message(text=self._get_current_time())
            elif topic:
                dispatcher.utter_message(text=COMPANY_KB.get(topic))
            elif self._is_company_category_query(user_message):
                dispatcher.utter_message(text=self._get_category_guide(user_message))
            else:
                dispatcher.utter_message(text="내부 지식에서는 해당 질문에 대한 답을 찾을 수 없어요.")
        elif mode == "gemini":
            self._call_gemini_api(dispatcher, user_message)
        else:
            dispatcher.utter_message(text="⚠️ 모드를 인식하지 못했어요. 다시 시도해주세요.")
        return []

    def _call_gemini_api(self, dispatcher: CollectingDispatcher, message: str):
        headers = {'Content-Type': 'application/json'}
        prompt = f"너는 '엔지켐생명과학'의 사내 업무를 도와주는 친절한 AI 비서야. 다음 질문에 대해 간결하고 명확하게 답변해줘. 질문: {message}"
        data = {"contents": [{"parts": [{"text": prompt}]}]}
        try:
            response = requests.post(API_URL, headers=headers, json=data, timeout=30)
            response.raise_for_status()
            text = response.json()['candidates'][0]['content']['parts'][0]['text']
            dispatcher.utter_message(text=text)
        except requests.exceptions.Timeout:
            dispatcher.utter_message(text="죄송합니다, 답변을 생성하는 데 너무 오래 걸려요.")
        except Exception:
            dispatcher.utter_message(text="죄송합니다. Gemini API 응답 중 오류가 발생했어요.")

    def _find_topic_by_keywords(self, message: str) -> str:
        keyword_mapping = {
            "오늘 날짜": "현재시간", "날짜": "현재시간", "오늘": "현재시간",
            "현재 시간": "현재시간", "시간": "현재시간", "지금 몇시": "현재시간", "몇시": "현재시간",
            "회사 이름": "회사이름", "회사명": "회사이름", "회사주소": "주소", "회사 주소": "주소",
            "회사 대표": "대표", "회사 회장": "대표", "대표이사": "대표",
            "회사 비전": "비전", "설립일": "설립일", "창립일": "설립일",
            "it팀": "IT팀", "자금팀": "자금팀", "보안팀": "보안팀", "복장 규정": "복장규정",
            "출근시간": "출근시간", "보안 규정": "보안규정"
        }
        sorted_keys = sorted(keyword_mapping.keys(), key=len, reverse=True)
        for keyword in sorted_keys:
            if keyword in message:
                return keyword_mapping[keyword]
        return ""

    def _get_current_time(self) -> str:
        seoul_time = datetime.now(pytz.timezone("Asia/Seoul"))
        return f"현재 한국 시간은 {seoul_time.strftime('%Y년 %m월 %d일 %A %p %I시 %M분')}입니다. 😊"

    def _is_company_category_query(self, message: str) -> bool:
        return any(word in message for word in ["부서", "팀", "업무", "프로세스", "신청", "복리", "후생", "규정", "정책", "연락처"])

    def _get_category_guide(self, message: str) -> str:
        if any(word in message for word in ["부서", "팀", "조직"]):
            return "어떤 부서 정보가 궁금하신가요? (예: R&D전략개발실, 국내영업팀, IT팀 등)"
        elif any(word in message for word in ["업무", "프로세스", "신청"]):
            return "어떤 업무 프로세스가 궁금하신가요? (예: 휴가신청, 출장신청 등)"
        elif any(word in message for word in ["복리", "후생", "혜택"]):
            return "복리후생 정보 중 어떤 부분이 궁금하신가요? (예: 점심시간, 주차, 연차수당 등)"
        else:
            return "안녕하세요! 회사 정보를 도와드릴게요. '회사 주소', '휴가 신청 방법'과 같이 질문해주시면 답변해드릴 수 있습니다."

class ActionSetMode(Action):
    def name(self) -> Text:
        return "action_set_mode"

    def run(self, dispatcher: CollectingDispatcher,
            tracker: Tracker,
            domain: Dict[Text, Any]) -> List[Dict[Text, Any]]:

        raw_mode = tracker.get_slot("mode")

        # 한글 모드명 변환
        mode_map = {
            "내부": "internal",
            "외부": "gemini",
            "Gemini": "gemini"
        }
        mode = mode_map.get(raw_mode, raw_mode)

        if mode == "internal":
            dispatcher.utter_message(response="utter_mode_set_internal")
        elif mode == "gemini":
            dispatcher.utter_message(response="utter_mode_set_gemini")
        else:
            dispatcher.utter_message(text="⚠️ 모드를 인식하지 못했어요. 다시 시도해주세요.")

        return [SlotSet("mode", mode)]

class ActionSetMode(Action):
    def name(self) -> Text:
        return "action_set_mode"

    def run(self, dispatcher: CollectingDispatcher,
            tracker: Tracker,
            domain: Dict[Text, Any]) -> List[Dict[Text, Any]]:

        raw_mode = tracker.get_slot("mode")
