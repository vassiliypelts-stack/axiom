import os, sys
sys.stdout.reconfigure(encoding='utf-8')
os.chdir(os.path.dirname(__file__) or ".")

with open("index.html", "r", encoding="utf-8") as f:
    html = f.read()

# Direct unicode char replacement
html = html.replace("accMark(a){return a.session_alive===1?'\U0001f7e2':(a.session_alive===0?'\U0001f534':'⚪')}",
                     "accMark(a){var sd='sdot ';return '<span class=\"'+sd+(a.session_alive===1?'g':(a.session_alive===0?'r':'gy'))+'\"></span>'}")

# Also directly replace the accMark usage patterns
html = html.replace("${m.account_alive===1?'\U0001f7e2':(m.account_alive===0?'\U0001f534':'⚪')}",
                     "${m.account_alive===1?'<span class=\"sdot g\"></span>':(m.account_alive===0?'<span class=\"sdot r\"></span>':'<span class=\"sdot gy\"></span>')}")

html = html.replace("${m.account_alive===1?'\U0001f7e2':'⚪'}",
                     "${m.account_alive===1?'<span class=\"sdot g\"></span>':'<span class=\"sdot gy\"></span>'}")

# EVENT_IC - notification type icons
html = html.replace("campaign_start:\"▶\"", "campaign_start:\"▶\"")
# Just replace the ⛔ in EVENT_IC
html = html.replace("ban:\"⛔\"", "ban:\"⛔\"")

# ⛔ is ban:⛔ → replace the literal
html = html.replace("ban:\"⛔\",account_banned:\"⛔\"", "ban:\"⛔\",account_banned:\"⛔\"")

# Actually those unicode escapes are fine, don't touch EVENT_IC

# Replace the badge text in accCell
html = html.replace("${m.account_alive===1?'<span class=\"sdot g\"></span>':(m.account_alive===0?'<span class=\"sdot r\"></span>':'<span class=\"sdot gy\"></span>')} ${esc(m.account_label||m.account_username||('#'+m.account_id))}",
                     "${m.account_alive===1?'<span class=\"sdot g\"></span>':(m.account_alive===0?'<span class=\"sdot r\"></span>':'<span class=\"sdot gy\"></span>')} ${esc(m.account_label||m.account_username||('#'+m.account_id))}")

with open("index.html", "w", encoding="utf-8") as f:
    f.write(html)

# Count remaining high-codepoint chars
remaining = sum(1 for ch in html if 0x1F300 <= ord(ch) <= 0x1F9FF)
print(f"Remaining: {remaining}")
