from mlx_lm import load, stream_generate

LANG_NAMES = {"ja": "Japanese", "en": "English"}  # Hy-MT2 のプロンプトは言語の正式名で書く


class Translator:
    def __init__(self, model_id, pair):
        """pair: 判定した言語 → 翻訳先の言語(例: {"ja": "en", "en": "ja"})

        プロンプトの形式はモデルごとに違うので、model_id で選ぶ(PLaMo 3 / TranslateGemma / Hy-MT2)。
        """
        self.pair = pair
        name = model_id.lower()
        if "translategemma" in name:
            self.family = "gemma"
        elif "hy-mt" in name:
            self.family = "hy"
        else:
            self.family = "plamo"
        self.model, self.tokenizer = load(
            model_id,
            tokenizer_config={"trust_remote_code": True},
            model_config={"trust_remote_code": True},
            trust_remote_code=True,
        )
        if self.family == "gemma":
            # without this it runs on to max_tokens, repeating <end_of_turn>
            self.tokenizer.add_eos_token("<end_of_turn>")

    def _prompt(self, text, source_lang, target_lang, context):
        if self.family == "plamo":
            kwargs = {"target_lang": target_lang, "source_lang": source_lang}
            if context:
                kwargs["context"] = context
            if target_lang == "ja":
                kwargs["form"] = "polite"
            messages = [{"role": "user", "content": text}]
        elif self.family == "gemma":  # context is not supported
            kwargs = {}
            messages = [{"role": "user", "content": [{
                "type": "text", "source_lang_code": source_lang, "target_lang_code": target_lang, "text": text,
            }]}]
        else:  # context is not supported
            kwargs = {}
            messages = [{"role": "user", "content": (
                f"Translate the following text into {LANG_NAMES.get(target_lang, target_lang)}. Note that you should "
                f"only output the translated result without any additional explanation:\n\n{text}"
            )}]
        return self.tokenizer.apply_chat_template(messages, add_generation_prompt=True, tokenize=False, **kwargs)

    def translate(self, text, source_lang, context=None):
        prompt = self._prompt(text, source_lang, self.pair[source_lang], context)
        # generate() took ~5s per call here while stream_generate took ~1s
        ids = self.tokenizer.encode(prompt, add_special_tokens=self.family != "gemma")  # the template adds <bos>
        out = stream_generate(self.model, self.tokenizer, prompt=ids, max_tokens=256)
        return "".join(r.text for r in out).strip()
