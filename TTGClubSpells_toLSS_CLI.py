import argparse
import json
import os
import re
import requests
from bs4 import BeautifulSoup

school_mapping = {
    "Ограждение": "abj",
    "Вызов": "con",
    "Прорицание": "div",
    "Очарование": "enc",
    "Воплощение": "evo",
    "Иллюзия": "ill",
    "Некромантия": "nec",
    "Преобразование": "trs",
}

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

# --- Вспомогательные словари для анализа текста ---
DAMAGE_TYPES_MAPPING = {
    "излучением": "radiant", "огнём": "fire", "холодом": "cold", "молнией": "lightning",
    "звуком": "thunder", "кислотой": "acid", "ядом": "poison", "психической энергией": "psychic",
    "некротической энергией": "necrotic", "силовой энергией": "force", "дробящий": "bludgeoning",
    "колющий": "piercing", "рубящий": "slashing", "дробящего": "bludgeoning", 
    "колющего": "piercing", "рубящего": "slashing"
}

SAVE_ABILITIES_MAPPING = {
    "силы": "str", "ловкости": "dex", "телосложения": "con",
    "интеллекта": "int", "мудрости": "wis", "харизмы": "cha"
}

def clean_and_htmlify_ttg_tags(text):
    if not text: return ""
    text = text.strip().strip('"')
    if text in ["type", "attrs", "content", "list", "unordered", "bullet", "item"]: return ""
    if text == "смесь воды и песка" or text.startswith("компоненты:"): return ""
    
    # Удаляем {@br} и лишние переносы строк сайта
    text = text.replace("{@br}", " ")
    
    text = re.sub(r"\{@b\s+([^}]+)\}", r"<strong>\1</strong>", text)
    text = re.sub(r"\{@i\s+([^}]+)\}", r"<em>\1</em>", text)
    text = re.sub(r"\{@roll\s+([^}]+)\}", r"\1", text)
    text = re.sub(r"\{@glossary\s+([^|}]+)(?:\|[^}]+)?\}", r"\1", text)
    return text.strip(", ")

def get_refined_description(html_content):
    match_useful_part = re.search(r',\[\d+(?:,\d+)*\]\s*,\s*(.*?),\{"classes":', html_content)
    if not match_useful_part:
        match_useful_part = re.search(r',\[\d+\]\s*,\s*(.*?),\{"classes":', html_content)
    if not match_useful_part:
        return "<p>Описание не найдено.</p>", None
        
    useful_part = match_useful_part.group(1)
    raw_paragraphs = re.findall(r'"([^"]*)"', useful_part)
    
    desc_soup = BeautifulSoup("", "html.parser")
    higher_soup = BeautifulSoup("", "html.parser")
    current_ul = None
    
    # Флаг для игнорирования Сборника советов мудреца
    skip_sage_advice = False
    
    for p_text in raw_paragraphs:
        html_ready_text = clean_and_htmlify_ttg_tags(p_text)
        if not html_ready_text or len(html_ready_text) <= 3: continue
        if re.match(r'^[a-zA-Z0-9_-]+$', html_ready_text): continue

        # Проверяем, не начался ли сборник советов
        if "сборник советов мудреца" in html_ready_text.lower():
            skip_sage_advice = True
            continue

        is_higher_level_text = any(keyword in html_ready_text.lower() for keyword in [
            "когда вы достигаете", "выше 1", "выше 2", "выше 3", "выше 4", 
            "выше 5", "выше 6", "выше 7", "выше 8", "длиться дольше при использовании ячейки",
            "изменяется на уровнях", "вы можете выбрать одно дополнительное существо"
        ])

        # Если встретили текст апкаста, советы мудреца гарантированно закончились
        if is_higher_level_text:
            skip_sage_advice = False

        # Если мы сейчас внутри блока советов мудреца — пропускаем эту строку
        if skip_sage_advice:
            continue

        is_list_item = html_ready_text.startswith("<strong>")
        
        if is_higher_level_text:
            current_ul = None 
            temp_soup = BeautifulSoup(f"{html_ready_text}", "html.parser")
            higher_soup.append(temp_soup)
        else:
            if is_list_item:
                if current_ul is None:
                    current_ul = desc_soup.new_tag("ul")
                    desc_soup.append(current_ul)
                temp_soup = BeautifulSoup(f"<li>{html_ready_text}</li>", "html.parser")
                current_ul.append(temp_soup.li)
            else:
                current_ul = None
                temp_soup = BeautifulSoup(f"<p>{html_ready_text}</p>", "html.parser")
                desc_soup.append(temp_soup.p)
                
    desc_html = str(desc_soup) if desc_soup.contents else "<p>Описание отсутствует.</p>"
    higher_html = str(higher_soup) if higher_soup.contents else None
    return desc_html, higher_html

def convert_dice_ru_to_en(text):
    """Конвертирует русское написание кубов вроде '1к8' или '2к6' в '1d8', '2d6'"""
    if not text: return ""
    return re.sub(r'(\d+)[кKkK](\d+)', r'\1d\2', text)

def analyze_spell_mechanics(description, higher_levels_text):
    """
    Анализирует текст заклинания для определения actionType, спасбросков,
    кубиков урона/лечения и формулы масштабирования.
    """
    desc_lower = description.lower()
    action_type = "util"
    damage_parts = []
    save_info = {}
    scaling_formula = None

    # 1. Определение типа действия (actionType)
    if "спасбросок" in desc_lower or "преуспеть в спасброске" in desc_lower:
        action_type = "save"
        for ru_ability, en_ability in SAVE_ABILITIES_MAPPING.items():
            if f"спасброске {ru_ability}" in desc_lower or f"спасбросок {ru_ability}" in desc_lower:
                save_info["ability"] = en_ability
                break
    elif "рукопашн" in desc_lower and "ата" in desc_lower and "закл" in desc_lower:
        action_type = "msak"
    elif "дальнобойн" in desc_lower and "ата" in desc_lower and "закл" in desc_lower:
        action_type = "rsak"
    elif "восст" in desc_lower and "хит" in desc_lower:
        action_type = "heal"
    if "временны" in desc_lower and "хит" in desc_lower:
        action_type = "util"

    # 2. Поиск кубиков урона или лечения
    dice_matches = re.findall(r'(\d+[кKкK]\d+)\s*(?:уроно|урона|хитов|излучением|огнём|холодом|молнией|звуком|кислотой|ядом|психической|некротической|силовой|дробящего|колющего|рубящего)?', desc_lower)
    
    if dice_matches:
        primary_dice = convert_dice_ru_to_en(dice_matches[0])
        
        if "ваш модификатор заклинательной характеристики" in desc_lower:
            # Убеждаемся, что модификатор относится к урону/лечению, а не к чему-то ещё
            # Для этого проверим, идет ли упоминание модификатора недалеко от кубов
            primary_dice = f"{primary_dice} + @mod"
        
        detected_type = ""
        for ru_type, en_type in DAMAGE_TYPES_MAPPING.items():
            if ru_type in desc_lower:
                detected_type = en_type
                break
        
        if action_type == "heal":
            damage_parts.append([primary_dice, "healing"])
        elif detected_type:
            damage_parts.append([primary_dice, detected_type])
        else:
            damage_parts.append([primary_dice])
            
        if action_type == "util":
            action_type = "other"

    # 3. Извлечение формулы апкаста из текста повышения уровня
    if higher_levels_text:
        higher_lower = higher_levels_text.lower()
        scale_match = re.search(r'(?:увеличивается на|дополнительно)\s*(\d+[кKкK]\d+)', higher_lower)
        if scale_match:
            scaling_formula = convert_dice_ru_to_en(scale_match.group(1))

    return action_type, damage_parts, save_info, scaling_formula

def transform_to_lss(json_data):
    class_mapping = {
        "Изобретатель": "artificer", "Бард": "bard", "Жрец": "cleric",
        "Друид": "druid", "Паладин": "paladin", "Следопыт": "ranger",
        "Чародей": "sorcerer", "Колдун": "warlock", "Волшебник": "wizard"
    }
    lss_classes = [class_mapping.get(cls, cls.lower()) for cls in json_data.get("classes", [])]
    full_name = f"{json_data.get('name_ru', '')} [{json_data.get('name_en', '')}]"

    description_value = json_data.get("description", "")
    higher_levels = json_data.get("higher_levels", "")
    if higher_levels:
        if json_data.get("level", 0) > 0:
            description_value += f"<p><strong>Накладывание более высокой ячейкой.</strong> {higher_levels}</p>"
        else:
            description_value += f"<p><strong>Улучшение заговора.</strong> {higher_levels}</p>"

    action_type, damage_parts, save_info, scaling_formula = analyze_spell_mechanics(description_value, higher_levels)

    ct = json_data.get("casting_time", {})
    activation_type = "bonus" if ct.get("unit") == "bonus_action" else ct.get("unit", "action")
    condition_str = ct.get("condition").strip() if ct.get("condition") else None
    
    activation = {"type": activation_type, "cost": ct.get("value", 1), "condition": condition_str}

    # Исправлено "мгновенно": instant -> inst, убран value при мгновенном действии
    dur = json_data.get("duration", {})
    dur_unit = dur.get("unit", "inst")
    if dur_unit == "instant" or "мгнов" in str(dur).lower():
        dur_unit = "inst"
        
    duration = {"units": dur_unit}
    if dur_unit != "inst" and "value" in dur and dur["value"] > 0:
        duration["value"] = str(dur["value"])

    rng_data = json_data.get("range", "self")
    lss_range = {"value": None, "long": None, "units": "self"}
    if isinstance(rng_data, dict):
        lss_range["value"] = rng_data.get("value")
        unit = rng_data.get("unit")
        lss_range["units"] = "ft" if unit == "feet" else ("mile" if unit == "miles" else unit)
    elif rng_data in ["touch", "self"]:
        lss_range["units"] = rng_data

    comp = json_data.get("components", {})
    components = {}
    if comp.get("verbal"): components["vocal"] = True
    if comp.get("somatic"): components["somatic"] = True
    if comp.get("material"): components["material"] = True
    if dur.get("concentration"): components["concentration"] = True
    if ct.get("ritual"): components["ritual"] = True

    system_data = {
        "description": {"value": description_value},
        "source": {"book": json_data.get("source", {}).get("source_book", "PHB")},
        "activation": activation,
        "duration": duration,
        "target": {},
        "range": lss_range,
        "actionType": action_type,
        "level": json_data.get("level", 0),
        "school": json_data.get("school", ""),
        "components": components
    }

    if action_type in ["save", "msak", "rsak", "heal"]:
        system_data["target"]["type"] = "creature"

    if damage_parts:
        system_data["damage"] = {"parts": damage_parts}

    if action_type == "save" and save_info:
        system_data["save"] = save_info

    if higher_levels:
        scaling_data = {"mode": "cantrip" if json_data.get("level", 0) == 0 else "level"}
        if scaling_formula:
            scaling_data["formula"] = scaling_formula
        system_data["scaling"] = scaling_data

    lss_spell = {
        "name": full_name,
        "type": "spell",
        "system": system_data,
        "classes": lss_classes
    }

    if comp.get("material") and "material_description" in comp:
        lss_spell["system"]["materials"] = {"value": comp["material_description"]}

    return lss_spell

def json_to_markdown(json_data):
    school_ru_mapping = {"abj": "Ограждение", "con": "Вызов", "div": "Прорицание", "enc": "Очарование", "evo": "Воплощение", "ill": "Иллюзия", "nec": "Некромантия", "trs": "Преобразование"}
    time_units = {"action": "Действие", "bonus_action": "Бонусное действие", "reaction": "Реакция", "minute": "мин.", "hour": "час.", "day": "день", "instant": "Мгновенно", "permanent": "Пока не рассеется"}

    md = [f"# {json_data.get('name_ru', '')} [{json_data.get('name_en', '')}]"]
    level = json_data.get("level", 0)
    level_str = "Заговор" if level == 0 else f"{level}-й уровень"
    school_str = school_ru_mapping.get(json_data.get("school"), "")
    md.append(f"*{level_str}, {school_str}*" if school_str else f"*{level_str}*")
    md.append("")
    
    md.append("> **Время накладывания:** " + _parse_casting_time(json_data.get("casting_time", {}), time_units))
    md.append("> **Дистанция:** " + _parse_range(json_data.get("range", "")))
    md.append("> **Длительность:** " + _parse_duration(json_data.get("duration", {}), time_units))
    md.append("> **Компоненты:** " + _parse_components(json_data.get("components", {})))
    md.append("")

    desc = json_data.get("description", "").replace('<p>', '').replace('</p>', '')
    md.append(desc)
    md.append("")

    higher_levels = json_data.get("higher_levels", "")
    if higher_levels:
        clean_higher = higher_levels.replace('<p>', '').replace('</p>', '')
        md.append(f"**{'Накладывание более высокой ячейкой.' if level > 0 else 'Улучшение заговора.'}** {clean_higher}")
        md.append("")

    classes = json_data.get("classes", [])
    if classes:
        md.append("---\n")
        md.append(f"**Классы:** {', '.join(classes)}")

    return "\n".join(md)

def _parse_casting_time(ct, units):
    if not ct: return "-"
    unit, val = ct.get("unit", ""), ct.get("value", 1)
    if unit in ["action", "bonus_action"]: res = units.get(unit, "")
    elif unit == "reaction": res = f"Реакция, {ct.get('condition', '')}" if ct.get('condition') else "Реакция"
    else: res = f"{val} {units.get(unit, unit)}"
    if ct.get("ritual"): res += " (ритуал)"
    return res

def _parse_range(rng):
    if isinstance(rng, dict):
        unit = rng.get("unit", "")
        return f"{rng.get('value', '')} {'футов' if unit == 'feet' else 'миль' if unit == 'miles' else unit}"
    return "На себя" if rng == "self" else "Касание" if rng == "touch" else str(rng)

def _parse_duration(dur, units):
    if not dur: return "-"
    unit, val = dur.get("unit", ""), dur.get("value", 0)
    res = units.get(unit, "") if unit in ["instant", "permanent"] else f"{val} {units.get(unit, unit)}"
    return f"Концентрация, до {res}" if dur.get("concentration") else res

def _parse_components(comp):
    if not comp: return "-"
    parts = []
    if comp.get("verbal"): parts.append("Вербальный")
    if comp.get("somatic"): parts.append("Соматический")
    if comp.get("material"):
        m_desc = comp.get("material_description", "")
        parts.append(f"Материальный ({m_desc})" if m_desc else "Материальный")
    return ", ".join(parts)

def parse_spell(spell_input, output_dir, save_format):
    spell_name = spell_input.split("/")[-1].strip()
    url = f"https://new.ttg.club/spells/{spell_name}"
    
    print(f"Отправляем запрос к {url}...")
    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code != 200:
            print(f"Ошибка сервера ({response.status_code}) для заклинания {spell_name}")
            return None

        html_content = response.text
        separator = '<div class="flex items-center gap-1">'
        
        if separator in html_content:
            clean_text = separator + html_content.split(separator, 1)[1]
            if '<footer ' in clean_text:
                clean_text = clean_text.split('<footer ', 1)[0]
        else:
            print(f"Ошибка структуры HTML для {spell_name}")
            return None

        soup = BeautifulSoup(clean_text, "html.parser")
        json_file = {}
        
        json_file["name_ru"] = soup.find("h2", class_="cursor-pointer truncate text-2xl text-(--ui-text-highlighted)").text.strip()
        json_file["name_en"] = soup.find("span", class_="cursor-pointer truncate text-secondary").text.strip()
        
        source_container = soup.find("div", class_="flex gap-1")
        if source_container:
            spans = source_container.find_all("span")
            json_file["source"] = {}
            if len(spans) >= 2:
                json_file["source"]["source_book"] = spans[0].text.strip()
                json_file["source"]["source_type"] = spans[1].text.strip()
        
        level_text = soup.find("div", class_="flex flex-wrap gap-1 rounded-lg bg-elevated p-3 italic").find_all("span")[0].text.strip()
        if level_text == "Заговор,":
            json_file["level"] = 0
        elif level_text.endswith("уровень,"):
            level_number = re.search(r"(\d+)-й уровень,", level_text)
            if level_number: json_file["level"] = int(level_number.group(1))
        
        school_text = soup.find("div", class_="flex flex-wrap gap-1 rounded-lg bg-elevated p-3 italic").find_all("span")[1].text.strip()
        json_file["school"] = school_mapping.get(school_text)
        
        casting_time_text = soup.find("span", string="Время накладывания:").next_sibling.text.strip()
        ct_lower = casting_time_text.lower()
        json_file["casting_time"] = {}
        if "бонусное действие" in ct_lower:
            json_file["casting_time"].update({"value": 1, "unit": "bonus_action", "condition": casting_time_text.split("Бонусное действие, ")[1].strip().replace(".", "") if "Бонусное действие, " in casting_time_text else ""})
        elif "действие" in ct_lower:
            json_file["casting_time"].update({"value": 1, "unit": "action"})
        elif "реакция" in ct_lower:
            json_file["casting_time"].update({"value": 1, "unit": "reaction", "condition": casting_time_text.split("Реакция, ")[1].strip().replace(".", "") if "Реакция, " in casting_time_text else ""})
        elif "мин" in ct_lower:
            m = re.search(r"(\d+)\s*мин", ct_lower)
            if m: json_file["casting_time"].update({"value": int(m.group(1)), "unit": "minute"})
        elif "час" in ct_lower:
            h = re.search(r"(\d+)\s*час", ct_lower)
            if h: json_file["casting_time"].update({"value": int(h.group(1)), "unit": "hour"})
        if "ритуал" in ct_lower:
            json_file["casting_time"]["ritual"] = True

        distance_text = soup.find("span", string="Дистанция:").next_sibling.text.strip()
        if "На себя" in distance_text:
            json_file["range"] = "self"
        elif " фут" in distance_text:
            f = re.search(r"(\d+)\s* фут", distance_text)
            if f: json_file["range"] = {"value": int(f.group(1)), "unit": "feet"}
        elif " мил" in distance_text:
            m = re.search(r"(\d+)\s* мил", distance_text)
            if m: json_file["range"] = {"value": int(m.group(1)), "unit": "miles"}
        elif "Касание" in distance_text: json_file["range"] = "touch"
            
        duration_text = soup.find("span", string="Длительность:").next_sibling.text.strip().lower()
        json_file["duration"] = {}
        if "мгнов" in duration_text: json_file["duration"].update({"value": 0, "unit": "instant"})
        elif "мин" in duration_text:
            m = re.search(r"(\d+)\s*мин", duration_text)
            if m: json_file["duration"].update({"value": int(m.group(1)), "unit": "minute"})
        elif "час" in duration_text:
            h = re.search(r"(\d+)\s*час", duration_text)
            if h: json_file["duration"].update({"value": int(h.group(1)), "unit": "hour"})
        elif "день" in duration_text:
            d = re.search(r"(\d+)\s*день", duration_text)
            if d: json_file["duration"].update({"value": int(d.group(1)), "unit": "day"})
        elif "пока не рассеется" in duration_text: json_file["duration"].update({"value": 0, "unit": "permanent"})
        if "концентрац" in duration_text: json_file["duration"]["concentration"] = True
        
        components_text = soup.find("span", string="Компоненты:").next_sibling.text.strip().lower()
        json_file["components"] = {}
        if "верб" in components_text: json_file["components"]["verbal"] = True
        if "сомат" in components_text: json_file["components"]["somatic"] = True
        if "матер" in components_text:
            json_file["components"]["material"] = True
            m_match = re.search(r"\(([^)]+)\)", components_text)
            if m_match: json_file["components"]["material_description"] = m_match.group(1).strip()
        
        desc_html, higher_html = get_refined_description(html_content)
        json_file["description"] = desc_html
        if higher_html: json_file["higher_levels"] = higher_html
        
        json_file["classes"] = []
        classes_label = soup.find("span", string=re.compile(r"Классы:\s*"))
        if classes_label and classes_label.parent:
            class_links = classes_label.parent.find_all("a", href=re.compile(r"/classes/"))
            for link in class_links:
                c_name = re.sub(r"\s*\[.*?\]", "", link.text.strip())
                if c_name and c_name not in json_file["classes"]: json_file["classes"].append(c_name)

        lss_json_data = transform_to_lss(json_file)
        
        safe_name = json_file['name_ru'].replace("/", "-").replace("\\", "-")
        os.makedirs(output_dir, exist_ok=True)

        if save_format in ["lss", "all"]:
            file_path = os.path.join(output_dir, f"{safe_name}_LSS.json")
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump([lss_json_data], f, ensure_ascii=False, indent=2)
            print(f"-> Сохранено (LSS JSON): {file_path}")

        if save_format in ["md", "all"]:
            markdown_text = json_to_markdown(json_file)
            file_path = os.path.join(output_dir, f"{safe_name}.md")
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(markdown_text)
            print(f"-> Сохранено (Markdown): {file_path}")

        return lss_json_data

    except Exception as e:
        print(f"Ошибка при обработке {spell_name}: {e}")
        return None

def main():
    parser = argparse.ArgumentParser(
        description="CLI-парсер заклинаний с сайта ttg.club в форматы LSS JSON и Markdown."
    )
    
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("-s", "--spell", help="Имя заклинания или его полный URL")
    group.add_argument("-f", "--file", help="Путь к txt-файлу со списком заклинаний")

    parser.add_argument("-o", "--output", default=".", help="Директория для сохранения")
    parser.add_argument(
        "-fmt", "--format", 
        choices=["lss", "md", "all", "json-array"], 
        default="lss", 
        help="Формат вывода: 'lss' (поштучный JSON), 'md' (Markdown), 'all' (оба поштучно) или 'json-array' (всё в один общий JSON файл)."
    )

    args = parser.parse_args()

    spells_to_parse = []
    if args.spell:
        spells_to_parse.append(args.spell)
    elif args.file:
        if not os.path.exists(args.file):
            print(f"Ошибка: Файл {args.file} не найден.")
            return
        with open(args.file, "r", encoding="utf-8") as f:
            spells_to_parse = [line.strip() for line in f if line.strip()]

    print(f"Найдено заклинаний для обработки: {len(spells_to_parse)}\n")
    
    all_spells_collected = []

    for index, spell in enumerate(spells_to_parse, 1):
        print(f"[{index}/{len(spells_to_parse)}] ", end="")
        spell_data = parse_spell(spell, args.output, args.format)
        
        if spell_data and args.format == "json-array":
            all_spells_collected.append(spell_data)

    if args.format == "json-array" and all_spells_collected:
        os.makedirs(args.output, exist_ok=True)
        final_file_path = os.path.join(args.output, "all_spells_LSS.json")
        with open(final_file_path, "w", encoding="utf-8") as f:
            json.dump(all_spells_collected, f, ensure_ascii=False, indent=2)
        print(f"\n[Успех] Все заклинания ({len(all_spells_collected)} шт.) объединены и сохранены в: {final_file_path}")

if __name__ == "__main__":
    main()
