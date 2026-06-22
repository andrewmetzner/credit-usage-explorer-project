import re

import pandas as pd


def format_model_name(model_str: str) -> str:
    if not model_str or model_str == "N/A":
        return "N/A"

    model_str = re.sub(r"_v_\d+$", "", model_str)
    model_str = re.sub(r"_v\d+$", "", model_str)

    parts = model_str.split("_")
    formatted_parts = []

    for part in parts:
        if part.lower() in ["gpt", "o3", "o1"]:
            formatted_parts.append(part.upper())
        elif part.lower() in [
            "fast", "mini", "pro", "reasoning", "completion",
            "audio", "imagegen", "deep", "research", "codex",
        ]:
            formatted_parts.append(part.capitalize())
        else:
            formatted_parts.append(part)

    result = " ".join(formatted_parts)
    result = re.sub(r"(?<=\d)\s+(?=\d)", ".", result)
    result = re.sub(r"\s+", " ", result).strip()

    return result if result else "N/A"


def parse_usage_type(usage_type_str: str) -> dict:
    if not usage_type_str or pd.isna(usage_type_str):
        return {
            "original": "N/A",
            "type": "N/A",
            "model_and_num": "N/A",
            "date": "N/A",
            "medium": "N/A",
            "io": "N/A",
        }

    original = str(usage_type_str).strip()

    io_val = "N/A"
    if "cached_input" in original or "input" in original:
        io_val = "input"
    elif "cached_output" in original or "output" in original:
        io_val = "output"

    medium = "N/A"
    if "text" in original:
        medium = "text"
    elif "voice" in original:
        medium = "voice"
    elif "audio" in original:
        medium = "audio"

    type_val = "N/A"
    model_and_num = "N/A"
    date_val = "N/A"

    date_match = re.search(r"(\d{4})_(\d{2})_(\d{2})", original)
    if date_match:
        date_val = f"{date_match.group(1)}-{date_match.group(2)}-{date_match.group(3)}"

    if original == "codex":
        type_val = "codex"

    elif original.startswith("api.codex"):
        type_val = "codex"
        match = re.match(r"api\.codex_(.+?)(?:_\d{4}_\d{2}_\d{2}|_text|_voice|$)", original)
        model_and_num = (
            f"{format_model_name(match.group(1))} (API)" if match else "Codex (API)"
        )

    elif original.startswith("api.gpt"):
        type_val = "codex" if "codex" in original else "chat"
        match = re.match(r"api\.gpt_(.+?)(?:_\d{4}_\d{2}_\d{2}|_text|_voice|$)", original)
        model_and_num = (
            f"{format_model_name(f'gpt_{match.group(1)}')} (API)" if match else "GPT (API)"
        )

    elif original.startswith("chat."):
        type_val = "chat"
        model_and_num = format_model_name(original[5:])

    elif original.startswith("voice."):
        type_val = "voice"
        model_and_num = format_model_name(original[6:])

    elif original.startswith("chat_tool."):
        type_val = "tool"
        model_and_num = format_model_name(original[10:])

    elif original.startswith("deep_research."):
        type_val = "deep_research"
        model_and_num = format_model_name(original[14:])

    return {
        "original": original,
        "type": type_val,
        "model_and_num": model_and_num,
        "date": date_val,
        "medium": medium,
        "io": io_val,
    }
