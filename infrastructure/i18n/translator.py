"""
i18n движок на базе python-fluent.
Фикс БАГ 4: FluentBundle из fluent.runtime, FluentParser из fluent.syntax.
"""
import logging
from pathlib import Path

from fluent.runtime import FluentBundle
from fluent.syntax import FluentParser

from infrastructure.database.models import Language

logger = logging.getLogger(__name__)

LOCALES_DIR   = Path(__file__).parent.parent.parent / "locales"
FALLBACK_LANG = Language.EN

FALLBACK_CHAIN: dict[Language, list[Language]] = {
    Language.RU: [Language.RU, Language.EN],
    Language.UZ: [Language.UZ, Language.EN],
    Language.EN: [Language.EN],
}

_parser = FluentParser(with_spans=False)


class Translator:
    def __init__(self) -> None:
        self._bundles: dict[Language, FluentBundle] = {}
        self._load_all()

    def _load_all(self) -> None:
        for lang in Language:
            ftl_path = LOCALES_DIR / f"{lang.value}.ftl"
            if not ftl_path.exists():
                logger.warning("Locale file not found: %s", ftl_path)
                continue

            bundle = FluentBundle([lang.value])
            ast    = _parser.parse(ftl_path.read_text(encoding="utf-8"))
            errors = bundle.add_resource(ast)

            for err in errors:
                logger.warning("Fluent error in %s: %s", ftl_path.name, err)

            self._bundles[lang] = bundle
            logger.info("Locale loaded: %s", lang.value)

    def get(self, lang: Language, key: str, **kwargs) -> str:
        for fallback_lang in FALLBACK_CHAIN.get(lang, [Language.EN]):
            bundle = self._bundles.get(fallback_lang)
            if not bundle:
                continue
            msg = bundle.get_message(key)
            if msg and msg.value:
                val, errors = bundle.format_pattern(msg.value, kwargs)
                if errors:
                    logger.debug("Fluent format errors %r: %s", key, errors)
                return str(val)

        logger.warning("Missing translation key: %r (lang=%s)", key, lang.value)
        return key

    def reload(self) -> None:
        self._bundles.clear()
        self._load_all()
        logger.info("Translations reloaded")


translator = Translator()


def t(lang: Language | str, key: str, **kwargs) -> str:
    if isinstance(lang, str):
        try:
            lang = Language(lang)
        except ValueError:
            lang = FALLBACK_LANG
    return translator.get(lang, key, **kwargs)
