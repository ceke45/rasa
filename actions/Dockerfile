FROM rasa/rasa-sdk:3.6.1

USER root

WORKDIR /app/actions

COPY actions.py ./
COPY requirements.txt ./

RUN pip install --no-cache-dir -r requirements.txt

CMD ["python", "-m", "rasa_sdk", "--actions", "actions"]
