# Записи голосов для озвучки

Сюда кладутся записи-референсы: по одной на каждую эмоцию, отдельно женский и мужской голос.
Что и как говорить — [docs/voice-phrases.md](../docs/voice-phrases.md).

```
voices/female/neutral.wav   voices/male/neutral.wav    нейтрально (самая важная)
voices/female/anger.wav     voices/male/anger.wav      злость
voices/female/sad.wav       voices/male/sad.wav        обида / грусть
voices/female/sarcasm.wav   voices/male/sarcasm.wav    сарказм
voices/female/anxious.wav   voices/male/anxious.wav    тревога / паника
voices/female/shock.wav     voices/male/shock.wav      шок / удивление
voices/female/joy.wav       voices/male/joy.wav        радость
voices/female/tired.wav     voices/male/tired.wav      усталость / разочарование
voices/female/warm.wav      voices/male/warm.wav       тепло / нежность
voices/female/guilty.wav    voices/male/guilty.wav     оправдание / вина
```

- Формат: wav (подойдут и mp3 / flac / ogg / m4a — с тем же именем), 7–10 с живой речи.
  Тишина по краям и разная громкость выравниваются автоматически.
- DeepSeek помечает каждую реплику эмоцией, озвучка берёт запись этой эмоции для пола героя.
  Нет записи — берётся `neutral.wav`, нет и её — любая запись из папки, пустая папка — встроенный
  голос Chatterbox.
- Проверить, что всё названо правильно:
  `venv\Scripts\python -m tts voices`
- Послушать одну фразу всеми эмоциями:
  `venv\Scripts\python -m tts emotions "Короче, ты не поверишь, что сегодня было." --gender male`
  → `output/tts/emotions/male/*.wav`

Записи в git не попадают (`voices/.gitignore`) — на другую машину (целевую) их нужно скопировать
вручную в такую же папку.
