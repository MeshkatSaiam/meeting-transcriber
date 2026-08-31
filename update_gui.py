import sys

with open('gui.py', 'r', encoding='utf-8') as f:
    content = f.read()

replacements = {
    '🔊 ': '',
    '📅 ': 'Date: ',
    '⏱ ': '',
    '⏳ ': 'Dur: ',
    '▷ Play': '> Play',
    '▶ Play': '> Play',
    '✏ Edit': 'Edit',
    '🗑 Delete': 'Delete',
    '👁 ': '',
    '📋 ': '',
    '📝 ': '',
    '📄 ': '',
    '🎙 ': '',
    '📂 ': '',
    '⬆ ': '',
    '⏹ Stop': 'Stop',
    '✨ Enhance': 'Enhance',
    '✂ Save': 'Save',
    ' ↗': '',
    '🔄 ': '',
    ' ∇': '',
    ' ⌄': '',
    '● Ready': 'Ready',
    'Config.set("graphics", "width", "1060")': 'Config.set("graphics", "width", "900")',
    'Config.set("graphics", "height", "820")': 'Config.set("graphics", "height", "700")',
    'Config.set("graphics", "minimum_width", "960")': 'Config.set("graphics", "minimum_width", "800")',
    'Config.set("graphics", "minimum_height", "700")': 'Config.set("graphics", "minimum_height", "600")',
    'font_size="18sp"': 'font_size="16sp"',
    'height=38': 'height=32',
}

for k, v in replacements.items():
    content = content.replace(k, v)

with open('gui.py', 'w', encoding='utf-8') as f:
    f.write(content)

print("Replacements complete!")
