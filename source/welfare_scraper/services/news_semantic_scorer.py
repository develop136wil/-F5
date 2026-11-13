"""Pinned embedding adapter for the opt-in news semantic gate."""
import asyncio
from dataclasses import dataclass
import hashlib
import json
import math

from services import ai_coordinator
from services.ai_input_safety import ensure_ai_text_safe
from services.ai_generation_safety import AiResponseReviewRequired
from services.embedding_input_limits import require_embedding_input


@dataclass(frozen=True)
class EmbeddingRecipe:
    model: str
    dimensions: int = 768
    max_chars: int = 8000

    def __post_init__(self):
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError('Explicit embedding model required')
        if type(self.dimensions) is not int or not 1 <= self.dimensions <= 3072:
            raise ValueError('Invalid embedding dimension')
        if type(self.max_chars) is not int or not 1 <= self.max_chars <= 8000:
            raise ValueError('Invalid embedding input limit')

    @property
    def version(self):
        # Keep the pinned vector recipe byte-compatible for existing runs.
        # The stricter length rule is an admission policy: every accepted input
        # is complete and was also untruncated under this exact old max_chars.
        # Both the live reference cache and gate's persisted-result reuse must
        # call review_inputs first; unsupported old prefixes are never reused.
        settings = [self.model, self.dimensions, self.max_chars, 'SEMANTIC_SIMILARITY', 'cosine-v1']
        return 'news-embedding-v1:' + hashlib.sha256(json.dumps(settings).encode()).hexdigest()


class SemanticScorer:
    def __init__(self, client, recipe):
        if client is None or not isinstance(recipe, EmbeddingRecipe):
            raise ValueError('Explicit client and recipe required')
        self._client, self._recipe = client, recipe
        self._reference = None  # one successful reference only, bounded memory
        self._lock = asyncio.Lock()

    @property
    def scoring_version(self):
        return self._recipe.version

    def review_inputs(self, reference, title):
        """Full-input preflight, including before reusing a persisted score."""
        if not isinstance(reference, str) or not isinstance(title, str):
            raise ValueError('Text inputs required')
        ensure_ai_text_safe(reference, title)
        require_embedding_input(reference, max_chars=self._recipe.max_chars)
        require_embedding_input(title, max_chars=self._recipe.max_chars)
        if not reference.strip() or not title.strip():
            raise ValueError('Nonempty text required')

    async def _embed(self, text):
        ensure_ai_text_safe(text)
        require_embedding_input(text, max_chars=self._recipe.max_chars)
        from google.genai import types
        recipe = self._recipe
        await ai_coordinator.acquire_model_slot(recipe.model,
            input_tokens=ai_coordinator.estimate_input_tokens(text))
        try:
            response = await ai_coordinator.await_api(self._client.aio.models.embed_content(
                model=recipe.model, contents=text,
                config=types.EmbedContentConfig(task_type='SEMANTIC_SIMILARITY', output_dimensionality=recipe.dimensions)))
        except Exception as error:
            ai_coordinator.register_api_error(recipe.model, error)
            raise
        embeddings = getattr(response, 'embeddings', None)
        if not isinstance(embeddings, (list, tuple)) or len(embeddings) != 1:
            raise AiResponseReviewRequired('incomplete')
        vector = getattr(embeddings[0], 'values', None)
        if (not isinstance(vector, (list, tuple)) or len(vector) != recipe.dimensions
                or any(type(v) not in (int, float) or not math.isfinite(v) for v in vector)):
            raise AiResponseReviewRequired('incomplete')
        norm = math.hypot(*vector)
        if not math.isfinite(norm) or norm == 0:
            raise AiResponseReviewRequired('incomplete')
        return tuple(v / norm for v in vector)

    async def __call__(self, reference, title):
        # Inspect both full inputs before admission or even the first reference
        # request. Sensitive title tails must not spend quota or warm the cache.
        self.review_inputs(reference, title)
        async with self._lock:
            if self._reference is None or self._reference[0] != reference:
                self._reference = (reference, await self._embed(reference))
            vector = await self._embed(title)
            score = math.fsum(a * b for a, b in zip(self._reference[1], vector))
            return max(-1.0, min(1.0, score))


async def process_with_embeddings(run_id, key, *, scorer, threshold, analysis_model, **kwargs):
    from services.news_semantic_gate import process_candidate
    if not isinstance(scorer, SemanticScorer):
        raise ValueError('Pinned semantic scorer required')
    return await process_candidate(run_id, key, scorer=scorer, threshold=threshold,
        analysis_model=analysis_model, scoring_version=scorer.scoring_version, **kwargs)
