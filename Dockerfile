# 使用官方輕量級 Python 映像檔
FROM python:3.10-slim

# 設定工作目錄
WORKDIR /app

# 複製依賴清單並安裝套件
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 複製專案其餘程式碼
COPY . .

# 設定對外連接埠（Cloud Run 預設會使用 8080）
ENV PORT=8080
EXPOSE 8080

# 直接使用 python 執行您的程式
CMD ["python", "app.py"]
