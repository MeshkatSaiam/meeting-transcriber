with open('gui.py', 'r', encoding='utf-8') as f:
    text = f.read()

text = text.replace('width=150,\n                halign="left",\n                valign="middle"\n            )\n            added_meta.bind', 'width=180,\n                halign="left",\n                valign="middle"\n            )\n            added_meta.bind')

text = text.replace('height=68, padding=[10, 8, 10, 8]', 'height=72, padding=[10, 8, 10, 8]')

with open('gui.py', 'w', encoding='utf-8') as f:
    f.write(text)

print("Widths updated.")
