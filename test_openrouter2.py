import json
d = json.load(open('or_dump.json', encoding='utf-8'))
img_data = d['choices'][0]['message']['images'][0]
if isinstance(img_data, str):
    print("It's a string starting with:", img_data[:50])
else:
    print("It's a dict with keys:", list(img_data.keys()))
