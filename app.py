# app.py
import os
from flask import Flask, send_from_directory, request, jsonify
from werkzeug.utils import secure_filename
import requests

app = Flask(__name__)

# ── 직접 설정(원하면 여기 값만 바꾸세요) ─────────────────────────────────────
RASA_URL  = "http://localhost:5005"  # Rasa 서버 REST webhook/API 주소
SENDER_ID = "local-user"             # 사용자 식별자(단일 사용자면 고정 OK)
UPLOAD_DIR = "uploads"               # 업로드 저장 디렉터리
# ────────────────────────────────────────────────────────────────────────────

os.makedirs(UPLOAD_DIR, exist_ok=True)


# 정적 파일(index.html 등) 서빙
@app.route("/")
def serve_index():
    return send_from_directory(".", "index.html")

@app.route("/<path:path>")
def serve_file(path):
    return send_from_directory(".", path)


# 파일 업로드 + 요약 트리거
@app.route("/upload", methods=["POST"])
def upload():
    """
    프런트에서 <input type="file" name="file"> 로 전송:
      1) 파일 저장
      2) Rasa /trigger_intent 로 file_uploaded 인텐트 발생(슬롯=파일경로/MIME)
      3) /webhooks/rest/webhook 에 /file_uploaded 메시지 → rules에 의해 action_summarize_file 실행
      4) 봇의 요약 메시지들을 JSON으로 반환
    """
    if "file" not in request.files:
        return jsonify({"ok": False, "msg": "file 필드가 없습니다."}), 400

    f = request.files["file"]
    if f.filename == "":
        return jsonify({"ok": False, "msg": "파일명이 비어있습니다."}), 400

    # 저장
    filename = secure_filename(f.filename)
    save_path = os.path.join(UPLOAD_DIR, filename)
    try:
        f.save(save_path)
    except Exception as e:
        return jsonify({"ok": False, "msg": f"파일 저장 실패: {e}"}), 500

    mime = f.mimetype or "application/octet-stream"

    # 1) file_uploaded 인텐트 트리거해 슬롯 세팅
    try:
        trigger_payload = {
            "name": "file_uploaded",
            "entities": {
                "uploaded_file_path": save_path,
                "uploaded_file_mime": mime,
            },
        }
        r = requests.post(
            f"{RASA_URL}/conversations/{SENDER_ID}/trigger_intent",
            json=trigger_payload,
            timeout=30,
        )
        if r.status_code >= 300:
            return jsonify({"ok": False, "msg": f"Rasa trigger 실패: {r.text}"}), 500
    except requests.exceptions.RequestException as e:
        return jsonify({"ok": False, "msg": f"Rasa trigger 예외: {e}"}), 500

    # 2) 요약 실행(규칙: file_uploaded → action_summarize_file)
    try:
        msg_payload = {"sender": SENDER_ID, "message": "/file_uploaded"}
        r2 = requests.post(
            f"{RASA_URL}/webhooks/rest/webhook",
            json=msg_payload,
            timeout=90
        )
        if not r2.ok:
            return jsonify({"ok": False, "msg": f"Rasa 응답 실패: {r2.text}"}), 500

        replies = []
        for m in r2.json():
            if "text" in m:
                replies.append(m["text"])

        return jsonify({
            "ok": True,
            "path": save_path,
            "mime": mime,
            "replies": replies
        })

    except requests.exceptions.RequestException as e:
        return jsonify({"ok": False, "msg": f"Rasa 요청 예외: {e}"}), 500


# 일반 텍스트 메시지 → Rasa로 전달
@app.route("/send", methods=["POST"])
def send():
    """
    JSON: { "text": "사용자 메시지" }
    → Rasa REST webhook으로 전달 후 응답 반환
    """
    data = request.get_json(force=True, silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"ok": False, "msg": "text가 비었습니다."}), 400

    try:
        payload = {"sender": SENDER_ID, "message": text}
        r = requests.post(
            f"{RASA_URL}/webhooks/rest/webhook",
            json=payload,
            timeout=60
        )
        if not r.ok:
            return jsonify({"ok": False, "msg": f"Rasa 응답 실패: {r.text}"}), 500

        return jsonify(r.json())

    except requests.exceptions.RequestException as e:
        return jsonify({"ok": False, "msg": f"Rasa 요청 예외: {e}"}), 500


# 헬스체크(옵션)
@app.route("/health")
def health():
    return jsonify({"ok": True})


if __name__ == "__main__":
    # Docker 없이 로컬 실행
    app.run(host="0.0.0.0", port=8080, debug=True)
