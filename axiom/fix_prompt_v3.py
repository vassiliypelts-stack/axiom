"""Update campaign 9404 with new prompt v3 + message_template"""
from db import database

database.init_db()

path = r"G:\Мой диск\2.Нейробизнес\2@ВАЙБКОДИНГ\My-Projects-Claude\Marketing&Sales\5 Единичек Гребенюк - воронка Р Брансона\ПРОМПТ — Ассистент Олега Аверса (для AXIOM тест).md"
with open(path, "r", encoding="utf-8") as f:
    prompt = f.read()

msg = (
    "{name}, добрый день! Я ассистент Олега Аверса. "
    "Олег 12 лет строит системы продаж для строек ИЖС, "
    "помог 14 компаниям выстроить партнёрские программы "
    "с риелторами (akademiyaizhs.ru). "
    "Он просил пригласить Вас на бесплатную 20-минутную "
    "диагностику: посмотрим где теряются заказы сейчас "
    "и как это закрыть. Когда удобно созвониться?"
)

with database.get_conn() as conn:
    conn.execute("UPDATE campaigns SET agent_prompt=?, message_template=? WHERE id=?",
                 (prompt, msg, 9404))
    conn.commit()
    c = conn.execute("SELECT id, name, message_template, status FROM campaigns WHERE id=9404").fetchone()
    print(f"✅ #{c[0]} {c[1]} — обновлён")
    mt = c[2] or ''
    print(f"   message_template: {mt[:100]}...")
    print(f"   agent_prompt: {len(prompt)} символов")
