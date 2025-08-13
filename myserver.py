from flask import Flask          # นำเข้า Flask เพื่อสร้างเว็บเซิร์ฟเวอร์
from threading import Thread     # นำเข้า Thread เพื่อรันเซิร์ฟเวอร์แบบคู่ขนาน

app = Flask('')                   # สร้างแอป Flask เปล่าๆ

@app.route('/')                   # กำหนด route หลัก (หน้าแรก)
def home():
    return "Server is running!"   # เมื่อเปิดเว็บจะแสดงข้อความนี้

def run():
    app.run(host='0.0.0.0', port=8080)  
    # รัน Flask ให้เปิดรับทุก IP (0.0.0.0) บน port 8080

def server_on():
    t = Thread(target=run)        # สร้าง Thread ใหม่เพื่อรันฟังก์ชัน run()
    t.start()                     # เริ่มรัน Thread
