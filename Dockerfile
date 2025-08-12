FROM rasa/rasa:3.6.21

WORKDIR /app

COPY . /app

# 필요 시 requirements.txt 사용
# RUN pip install --no-cache-dir -r requirements.txt

CMD ["rasa", "run", "--enable-api", "--cors", "*", "--debug"]
