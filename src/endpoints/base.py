import pandas as pd
from typing import Callable, Coroutine, Any, Protocol, cast

from client import AsyncAGRClient

class Endpoint(Protocol):
    """An async endpoint function carrying its URL template.

    Produced by the ``@agr_endpoint`` decorator. Describing it as a Protocol
    lets the type checker see the ``url_template`` attribute that the decorator
    attaches at runtime.
    """
    url_template: str
    def __call__(
        self, gene_id: str, client: AsyncAGRClient
    ) -> Coroutine[Any, Any, tuple[pd.DataFrame, pd.DataFrame]]: ...

def agr_endpoint(
    url: str,
) -> Callable[[Callable[..., Coroutine[Any, Any, tuple[pd.DataFrame, pd.DataFrame]]]], Endpoint]:
    """Attach a URL template to an endpoint function.

    Usage:
        @agr_endpoint("/gene/{gene_id}/alleles")
        async def get_alleles(gene_id, client): ...

        get_alleles.url_template                               # "/gene/{gene_id}/alleles"
        get_alleles.url_template.format(gene_id="HGNC:1100")   # "/gene/HGNC:1100/alleles"
    """
    def decorator(func):
        func.url_template = url
        return cast(Endpoint, func)
    return decorator
