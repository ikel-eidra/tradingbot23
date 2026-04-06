FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Create data and log directories
RUN mkdir -p data logs

CMD ["python", "-m", "bot.main", "--mode", "paper"]
