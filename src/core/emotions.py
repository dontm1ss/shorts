"""Эмоции реплик: DeepSeek помечает ими реплики сценария (script[].emotion), озвучка берёт
референс voices/<пол>/<эмоция>.wav (docs/voice-phrases.md) и поправку подачи tts.emotions.

Список совпадает с enum в schema/scene.schema.json и с именами файлов записей."""
from __future__ import annotations

DEFAULT = "neutral"

# id → (подпись в панели, когда ставить — для промпта DeepSeek)
EMOTIONS = {
    "neutral": ("нейтрально", "спокойный живой рассказ, по умолчанию"),
    "anger": ("злость", "злость, раздражение, наезд"),
    "sad": ("грусть", "обида, грусть"),
    "sarcasm": ("сарказм", "сарказм, ирония, издёвка"),
    "anxious": ("тревога", "тревога, паника, страх"),
    "shock": ("шок", "шок, удивление, «что?!»"),
    "joy": ("радость", "радость, восторг, веселье"),
    "tired": ("усталость", "усталость, разочарование, «мне уже всё равно»"),
    "warm": ("тепло", "тепло, нежность, забота"),
    "guilty": ("вина", "оправдание, вина, неловкость"),
}

# Что DeepSeek иногда пишет вместо id
_ALIASES = {
    "calm": "neutral", "нейтрально": "neutral", "нейтральная": "neutral", "спокойно": "neutral",
    "angry": "anger", "rage": "anger", "annoyed": "anger", "злость": "anger", "злой": "anger", "гнев": "anger",
    "sadness": "sad", "hurt": "sad", "грусть": "sad", "обида": "sad",
    "sarcastic": "sarcasm", "irony": "sarcasm", "ironic": "sarcasm", "сарказм": "sarcasm", "ирония": "sarcasm",
    "anxiety": "anxious", "fear": "anxious", "panic": "anxious", "scared": "anxious",
    "тревога": "anxious", "страх": "anxious", "паника": "anxious",
    "surprise": "shock", "surprised": "shock", "shocked": "shock", "шок": "shock", "удивление": "shock",
    "happy": "joy", "happiness": "joy", "excited": "joy", "радость": "joy", "восторг": "joy",
    "tiredness": "tired", "disappointed": "tired", "disappointment": "tired", "усталость": "tired",
    "разочарование": "tired",
    "tender": "warm", "tenderness": "warm", "love": "warm", "тепло": "warm", "нежность": "warm",
    "guilt": "guilty", "ashamed": "guilty", "вина": "guilty", "оправдание": "guilty",
}


def normalize_emotion(value) -> str:
    """Эмоция из ответа DeepSeek → id из EMOTIONS; непонятное — neutral."""
    key = str(value or "").strip().lower()
    return key if key in EMOTIONS else _ALIASES.get(key, DEFAULT)


def emotion_list_for_prompt() -> str:
    return "\n".join(f"- {key} — {hint};" for key, (_, hint) in EMOTIONS.items())
