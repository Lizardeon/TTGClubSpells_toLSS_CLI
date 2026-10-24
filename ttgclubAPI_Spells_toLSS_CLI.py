import argparse
import json
import re
import requests
from pathlib import Path
from typing import Any

BASE_URL = "https://new.ttg.club/api/v2/spells"

# { @tag  содержимое_до_|  [ | хвост ] }
TAG_RE = re.compile(r"\{@(?P<tag>\w+)\s+(?P<body>[^{}]*?)\}")

# --- Словари ---

SCHOOL_MAPPING = {
    "Ограждение": "abj",
    "Вызов": "con",
    "Прорицание": "div",
    "Очарование": "enc",
    "Воплощение": "evo",
    "Иллюзия": "ill",
    "Некромантия": "nec",
    "Преобразование": "trs",
}

CLASS_MAPPING = {
    "Изобретатель": "artificer",
    "Бард": "bard",
    "Жрец": "cleric",
    "Друид": "druid",
    "Паладин": "paladin",
    "Следопыт": "ranger",
    "Чародей": "sorcerer",
    "Колдун": "warlock",
    "Волшебник": "wizard",
}

DAMAGE_TYPES_MAPPING = {
    # Формы, которые встречаются в тексте
    "излучением": "radiant", "излучению": "radiant",
    "огнём": "fire", "огнем": "fire", "огню": "fire",
    "холодом": "cold", "холоду": "cold",
    "молнией": "lightning", "молнии": "lightning",
    "звуком": "thunder", "звуку": "thunder",
    "кислотой": "acid", "кислоте": "acid",
    "ядом": "poison", "яду": "poison",
    "психической энергией": "psychic", "психической энергии": "psychic",
    "некротической энергией": "necrotic", "некротической энергии": "necrotic",
    "силовым полем": "force", "силовой энергией": "force", "силовой энергии": "force",
    "дробящего": "bludgeoning", "дробящий": "bludgeoning", "дробящим": "bludgeoning",
    "дробящую": "bludgeoning", "дробящей": "bludgeoning",
    "колющего": "piercing", "колющий": "piercing", "колющим": "piercing",
    "колющую": "piercing", "колющей": "piercing",
    "рубящего": "slashing", "рубящий": "slashing", "рубящим": "slashing",
    "рубящую": "slashing", "рубящей": "slashing",
}

SAVE_ABILITIES_MAPPING = {
    "силы": "str",
    "ловкости": "dex",
    "телосложения": "con",
    "интеллекта": "int",
    "мудрости": "wis",
    "харизмы": "cha",
}


# --- Утилиты ---

def fetch_spell(spell_id: str) -> dict[str, Any]:
    """Загружает JSON заклинания по его идентификатору (с кэшем)."""
    url = f"{BASE_URL}/{spell_id}"

    cache_dir = Path("cache")
    cache_file = cache_dir / f"{spell_id}.json"
    cache_dir.mkdir(parents=True, exist_ok=True)

    if cache_file.is_file():
        try:
            with cache_file.open("r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            cache_file.unlink(missing_ok=True)

    response = requests.get(url, timeout=100)
    response.raise_for_status()
    data = response.json()

    with cache_file.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

    return data


def refine_link(spell_link: str) -> str:
    if "/" in spell_link:
        return spell_link.rstrip("/").split("/")[-1]
    return spell_link


def strip_tags(text: str, rules: dict[str, tuple[str, str]] | None = None) -> str:
    """
    rules: {tag: (prefix, suffix)}
    По умолчанию для всех — ("", "")
    """
    rules = rules or {}

    def repl(m: re.Match) -> str:
        tag = m.group("tag")
        word = m.group("body").split("|", 1)[0].strip()
        pre, suf = rules.get(tag, ("", ""))
        return f"{pre}{word}{suf}"

    return TAG_RE.sub(repl, text)


def convert_dice_ru_to_en(text: str) -> str:
    """'1к8' -> '1d8', '2К6' -> '2d6'"""
    if not text:
        return ""
    return re.sub(r'(\d+)\s*[кKkK]\s*(\d+)', r'\1d\2', text)


def strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "")


# --- Парсеры полей ---

def parse_activation(casting_time: str) -> dict:
    ct = casting_time.lower()
    result = {"type": "action", "cost": 1, "condition": ""}

    m = re.search(r"\d+", ct)
    if m:
        result["cost"] = int(m.group())

    if "бонусн" in ct:
        result["type"] = "bonus"
        if "," in casting_time:
            result["condition"] = casting_time.split(",", 1)[-1].strip().capitalize()
    elif "реакц" in ct:
        result["type"] = "reaction"
        if "," in casting_time:
            result["condition"] = casting_time.split(",", 1)[-1].strip().capitalize()
    elif "действ" in ct:
        result["type"] = "action"
    elif "час" in ct:
        result["type"] = "hour"
    elif "мин" in ct:
        result["type"] = "minute"
    elif "сек" in ct:
        result["type"] = "second"

    return result


RANGE_UNIT_PATTERNS = [
    (r"на\s+себя", "self", None),
    (r"касан", "touch", None),
    (r"неогранич", "any", None),
    (r"видим", "sight", None),
    (r"особ", "spec", None),
    (r"(\d+)\s*фут", "ft", 1),
    (r"(\d+)\s*фт", "ft", 1),
    (r"(\d+)\s*мил", "mile", 1),
]


def parse_range(range_text: str) -> dict:
    rt = range_text.lower()
    result = {"value": None, "long": None, "units": "self"}
    for pattern, unit, grp in RANGE_UNIT_PATTERNS:
        m = re.search(pattern, rt)
        if m:
            result["units"] = unit
            if grp:
                result["value"] = int(m.group(grp))
            elif unit == "self":
                result["value"] = 0
            break
    return result


AREA_TYPES_PATTERNS = [
    (r"конус(?:е|ом|а)?\s*(?:с\s+радиусом\s+)?(\d+)", "cone"),
    (r"конус(?:е|ом|а)?\s*(?:радиусом\s+)?(\d+)", "cone"),
    (r"сфер[аыуе]\s*(?:радиусом\s+)?(\d+)", "sphere"),
    (r"в\s+(\d+)[-\s]футов(?:ой|ая|ое)?\s+сфер", "sphere"),
    (r"куб[ае]?\s*(?:с\s+)?(?:гранью\s+)?(\d+)", "cube"),
    (r"цилиндр[ае]?\s*(?:с\s+)?(?:радиусом\s+)?(\d+)", "cylinder"),
    (r"лини[июя]\s*(?:длиной\s+)?(\d+)", "line"),
    (r"эманаци[еия]\s*(?:на\s+|в\s+)?(\d+)", "radius"),
    (r"(\d+)[-\s]футов(?:ой|ая|ое)?\s+эманац", "radius"),
    (r"радиусом\s+(\d+)", "radius"),
]


def detect_target_from_description(description_text: str) -> dict | None:
    dt = description_text.lower()
    for pattern, ttype in AREA_TYPES_PATTERNS:
        m = re.search(pattern, dt)
        if m:
            return {"value": int(m.group(1)), "width": None, "units": "ft", "type": ttype}
    return None


def parse_target(description_text: str, range_dict: dict, action_type: str) -> dict:
    area = detect_target_from_description(description_text)
    if area:
        return area

    units = range_dict.get("units")
    if units == "self":
        return {"value": None, "width": None, "units": "ft", "type": ""}
    if units in ("touch", "any", "sight", "spec"):
        return {"units": "ft", "type": "creature"}
    if action_type in ("save", "msak", "rsak", "heal"):
        return {"units": "ft", "type": "creature"}
    return {"value": None, "width": None, "units": "ft", "type": ""}


DURATION_UNITS_PATTERNS = [
    (r"мгновен", "inst", None),
    (r"раунд", "round", 1),
    (r"мин", "minute", 1),
    (r"час", "hour", 1),
    (r"день|дн", "day", 1),
    (r"постоян|пока не рассеется", "perm", None),
    (r"особ", "spec", None),
]


def parse_duration(duration_text: str) -> dict:
    dt = duration_text.lower()
    result = {"value": None, "units": "inst"}
    for pattern, unit, grp in DURATION_UNITS_PATTERNS:
        m = re.search(pattern, dt)
        if m:
            result["units"] = unit
            if grp:
                vm = re.search(r"(\d+)", dt)
                result["value"] = vm.group(1) if vm else "1"
            break
    return result


def parse_components(raw: dict, casting_time: str, duration_text: str):
    components = {
        "vocal": False,
        "somatic": False,
        "material": False,
        "ritual": False,
        "concentration": False,
    }
    materials = {"value": "", "consumed": False, "cost": 0, "supply": 0}

    api_components = raw.get("components")
    if isinstance(api_components, dict):
        components["vocal"] = bool(
            api_components.get("verbal") or api_components.get("vocal") or api_components.get("v")
        )
        components["somatic"] = bool(
            api_components.get("somatic") or api_components.get("s")
        )
        components["material"] = bool(
            api_components.get("material") or api_components.get("m")
        )
        md = (
            api_components.get("materialDescription")
            or api_components.get("material_description")
            or api_components.get("materialDescription")
            or ""
        )
        if md:
            materials["value"] = md
    elif isinstance(api_components, str):
        c = api_components.lower()
        if "в" in c:
            components["vocal"] = True
        if "с" in c:
            components["somatic"] = True
        if "м" in c:
            components["material"] = True

    if "ритуал" in casting_time.lower():
        components["ritual"] = True
    if "концентрац" in duration_text.lower():
        components["concentration"] = True

    if materials["value"]:
        cost_match = re.search(r"стоимостью\s+(\d+)", materials["value"])
        if cost_match:
            materials["cost"] = int(cost_match.group(1))
        if "расходуется" in materials["value"].lower():
            materials["consumed"] = True

    return components, materials


def analyze_mechanics(plain_description: str, plain_upper: str, level: int) -> dict:
    desc_lower = plain_description.lower()

    result = {
        "actionType": "util",
        "damage": {"parts": [], "versatile": ""},
        "save": {"ability": "", "dc": None, "scaling": "spell"},
        "scaling": {"mode": "none", "formula": ""},
    }

    # --- actionType ---
    has_save = "спасброс" in desc_lower
    has_msak = "рукопашн" in desc_lower and "атака заклинанием" in desc_lower
    has_rsak = "дальнобойн" in desc_lower and "атака заклинанием" in desc_lower
    has_heal = any(w in desc_lower for w in ["восстанавлива", "восстановить"])
    has_temp_hp = "временн" in desc_lower and "хит" in desc_lower

    if has_save:
        result["actionType"] = "save"
        for ru, en in SAVE_ABILITIES_MAPPING.items():
            if f"спасбросок {ru}" in desc_lower or f"спасброске {ru}" in desc_lower:
                result["save"]["ability"] = en
                break
    elif has_msak:
        result["actionType"] = "msak"
    elif has_rsak:
        result["actionType"] = "rsak"
    elif has_heal:
        result["actionType"] = "heal"
    elif has_temp_hp:
        result["actionType"] = "util"

    # --- Damage / Healing dice ---
    if result["actionType"] == "heal":
        heal_re = re.compile(
            r"(?:восстанавлива[ею]т|восстановить|восстанавливает)\s+"
            r"(\d+)\s*[кKkK]\s*(\d+)"
            r"(\s*\+\s*ваш\s+модификатор)?",
            re.IGNORECASE,
        )
        for m in heal_re.finditer(desc_lower):
            dice = f"{m.group(1)}d{m.group(2)}"
            if m.group(3):
                dice += " + @mod"
            result["damage"]["parts"].append([dice, "healing"])
    else:
        dmg_re = re.compile(
            r"(\d+)\s*[кKkK]\s*(\d+)"
            r"(\s*\+\s*(\d+))?"
            r"\s*(?:урона|уроно|урон)\s*([а-яё]+(?:\s+[а-яё]+)?)?",
            re.IGNORECASE,
        )
        seen = set()
        for m in dmg_re.finditer(desc_lower):
            dice = f"{m.group(1)}d{m.group(2)}"
            if m.group(3):
                dice += f" + {m.group(4)}"
            dtype_text = (m.group(5) or "").strip()
            dmg_type = ""
            best_len = 0
            for ru, en in DAMAGE_TYPES_MAPPING.items():
                if ru in dtype_text and len(ru) > best_len:
                    best_len = len(ru)
                    dmg_type = en
            if not dmg_type:
                ctx = desc_lower[max(0, m.start() - 100):m.end() + 100]
                for ru, en in DAMAGE_TYPES_MAPPING.items():
                    if ru in ctx and len(ru) > best_len:
                        best_len = len(ru)
                        dmg_type = en
            key = (dice, dmg_type)
            if key in seen:
                continue
            seen.add(key)
            if dmg_type:
                result["damage"]["parts"].append([dice, dmg_type])
            else:
                result["damage"]["parts"].append([dice])

    # Temp HP (без урона)
    if (
        result["actionType"] == "util"
        and has_temp_hp
        and not result["damage"]["parts"]
    ):
        temp_match = re.search(r"(\d+)\s*временн", desc_lower)
        if temp_match:
            result["damage"]["parts"].append([str(temp_match.group(1)), "temphp"])

    # --- Scaling ---
    if plain_upper:
        upper_lower = plain_upper.lower()
        scale_match = re.search(
            r"(?:увеличива[ею]тся|увеличивается|дополнительно)\s*(?:на\s*)?"
            r"(\d+[кKkK]\d+)",
            upper_lower,
        )
        if scale_match:
            result["scaling"] = {
                "mode": "cantrip" if level == 0 else "level",
                "formula": convert_dice_ru_to_en(scale_match.group(1)),
            }
        else:
            result["scaling"] = {
                "mode": "cantrip" if level == 0 else "level",
                "formula": "",
            }

        # Для заговоров — fallback на базовый куб урона
        if (
            level == 0
            and not result["scaling"]["formula"]
            and result["damage"]["parts"]
        ):
            base = result["damage"]["parts"][0][0]
            if re.match(r"\d+d\d+", base):
                result["scaling"]["formula"] = base

    return result


def build_description(raw: dict) -> tuple[str, str]:
    """Возвращает (HTML-описание, plain-text апкаста)."""
    desc_list = raw.get("description") or []
    upper_list = raw.get("upper") or []
    level = raw.get("level", 0)

    parts = [f"<p>{p}</p>" for p in desc_list]

    if upper_list:
        upper_html = ""
        for i, p in enumerate(upper_list):
            if i == 0:
                upper_html += p
            else:
                upper_html += f"</p><p>{p}"
        upper_html += "</p>"

        prefix = (
            "<p><strong>Улучшение заговора.</strong> "
            if level == 0
            else "<p><strong>Накладывание более высокой ячейкой.</strong> "
        )
        parts.append(prefix + upper_html)

    html_text = "".join(parts)

    rules = {
        "glossary": ("<em>", "</em>"),
        "roll": ("", ""),
        "b": ("<strong>", "</strong>"),
        "i": ("<em>", "</em>"),
        "br": (" ", ""),
        "hit": ("", ""),
        "dc": ("", ""),
        "damage": ("", ""),
        "dice": ("", ""),
        "chance": ("", ""),
        "recharge": ("", ""),
        "atk": ("", ""),
        "h": ("", ""),
        "note": ("", ""),
        "skill": ("", ""),
        "action": ("", ""),
        "condition": ("", ""),
        "spell": ("<em><strong>", "</strong></em>"),
        "item": ("", ""),
        "creature": ("", ""),
        "filter": ("", ""),
        "link": ("", ""),
    }

    stripped = strip_tags(html_text, rules)
    upper_plain = strip_tags(" ".join(upper_list)) if upper_list else ""

    return stripped, upper_plain


def build_spell(raw: dict) -> dict:
    ru_name = (raw.get("name") or {}).get("rus", "Неизвестно")
    en_name = (raw.get("name") or {}).get("eng", "Unknown")

    spell = {
        "name": f"{ru_name} [{en_name}]",
        "type": "spell",
        "system": {},
        "classes": [],
    }

    sys = spell["system"]

    # --- Описание ---
    desc_html, upper_plain = build_description(raw)
    plain_desc = strip_html(desc_html)
    sys["description"] = {"value": desc_html}

    # --- Источник ---
    source = raw.get("source") or {}
    src_name = source.get("name") or {}
    book = src_name.get("label") if isinstance(src_name, dict) else src_name
    sys["source"] = {"book": book or "PHB"}

    # --- Уровень, школа ---
    sys["level"] = raw.get("level", 0)
    sys["school"] = SCHOOL_MAPPING.get(raw.get("school"), "")

    # --- Активация ---
    casting_time = raw.get("castingTime", "1 действие")
    sys["activation"] = parse_activation(casting_time)

    # --- Дистанция ---
    range_text = raw.get("range", "На себя")
    rng = parse_range(range_text)
    sys["range"] = rng

    # --- Длительность ---
    duration_text = raw.get("duration", "Мгновенная")
    sys["duration"] = parse_duration(duration_text)

    # --- Компоненты ---
    components, materials = parse_components(raw, casting_time, duration_text)
    sys["components"] = components
    sys["materials"] = materials

    # --- Механика (actionType, save, damage, scaling) ---
    mech = analyze_mechanics(plain_desc, upper_plain, sys["level"])
    sys["actionType"] = mech["actionType"]
    sys["damage"] = mech["damage"]
    sys["save"] = mech["save"]
    sys["scaling"] = mech["scaling"]

    # --- Цель ---
    sys["target"] = parse_target(plain_desc, rng, mech["actionType"])

    # --- Служебные поля Foundry ---
    sys["uses"] = {"value": 0, "max": "0", "per": ""}
    sys["consume"] = {"type": "", "target": "", "amount": None}
    sys["ability"] = ""
    sys["critical"] = {"threshold": None, "damage": None}
    sys["formula"] = ""

    if sys["level"] == 0:
        sys["preparation"] = {"mode": "always", "prepared": True}
    else:
        sys["preparation"] = {"mode": "prepared", "prepared": False}

    # --- Классы ---
    classes_list = (raw.get("affiliation") or {}).get("classes") or []
    for cls in classes_list:
        url = cls.get("url", "")
        cls_id = url.split("-")[0] if url else ""
        if cls_id and cls_id not in spell["classes"]:
            spell["classes"].append(cls_id)

    return spell


# --- CLI ---

def main():
    parser = argparse.ArgumentParser(
        description="TTG Club API -> LSS JSON: полный конвертер заклинаний."
    )

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("-s", "--spell", help="URL заклинания или его ID")
    group.add_argument("-f", "--file", help="Файл со списком URL/ID (по одному в строке)")

    parser.add_argument(
        "-o", "--output", default=".", help="Директория для сохранения"
    )
    parser.add_argument(
        "-fmt", "--format",
        choices=["lss", "json-array"],
        default="lss",
        help="lss — по одному файлу на заклинание, json-array — всё в один файл",
    )

    args = parser.parse_args()

    if args.spell:
        spells = [args.spell]
    else:
        if not Path(args.file).is_file():
            print(f"Файл не найден: {args.file}")
            return
        with open(args.file, "r", encoding="utf-8") as f:
            spells = [line.strip() for line in f if line.strip()]

    print(f"Обработка {len(spells)} заклинаний...\n")

    all_spells = []
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    for i, link in enumerate(spells, 1):
        print(f"[{i}/{len(spells)}] {link}")
        try:
            spell_id = refine_link(link)
            raw = fetch_spell(spell_id)
            spell_data = build_spell(raw)
            all_spells.append(spell_data)

            if args.format == "lss":
                safe_name = re.sub(
                    r'[<>:"/\\|?*]', "-", (raw.get("name") or {}).get("rus", spell_id)
                )
                out_file = output_dir / f"{safe_name}_LSS.json"
                with open(out_file, "w", encoding="utf-8") as f:
                    json.dump([spell_data], f, ensure_ascii=False, indent=2)
                print(f"  -> {out_file}")
        except Exception as e:
            print(f"  ОШИБКА: {e}")

    if args.format == "json-array" and all_spells:
        out_file = output_dir / "all_spells_LSS.json"
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(all_spells, f, ensure_ascii=False, indent=2)
        print(f"\nСохранено {len(all_spells)} заклинаний в {out_file}")

if __name__ == "__main__":
    main()