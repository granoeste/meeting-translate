from mlx_lm import load, stream_generate

MODEL = "pfnet/plamo-3-translate-8b-MLX-4bit"
PAIR = {"ja": "en", "en": "ja"}


class Translator:
    def __init__(self, model_id=MODEL):
        self.model, self.tokenizer = load(
            model_id,
            tokenizer_config={"trust_remote_code": True},
            model_config={"trust_remote_code": True},
            trust_remote_code=True,
        )

    def translate(self, text, source_lang, context=None):
        kwargs = {"target_lang": PAIR[source_lang], "source_lang": source_lang}
        if context:
            kwargs["context"] = context
        if kwargs["target_lang"] == "ja":
            kwargs["form"] = "polite"
        prompt = self.tokenizer.apply_chat_template(
            [{"role": "user", "content": text}],
            add_generation_prompt=True,
            tokenize=False,
            **kwargs,
        )
        # generate() took ~5s per call here while stream_generate took ~1s
        ids = self.tokenizer.encode(prompt)
        out = stream_generate(self.model, self.tokenizer, prompt=ids, max_tokens=256)
        return "".join(r.text for r in out).strip()
