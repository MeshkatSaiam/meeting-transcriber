with open('gui.py', 'r', encoding='utf-8') as f:
    text = f.read()

text = text.replace('"█" * bars + "░" * (10 - bars)', '"|" * bars + "-" * (10 - bars)')

with open('gui.py', 'w', encoding='utf-8') as f:
    f.write(text)
print("done")
