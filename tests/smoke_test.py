"""Offline smoke test: imports the package and exercises the gene index and cache.

Run with `python tests/smoke_test.py`. Makes no network requests.
"""

import tempfile
from pathlib import Path

import pandas as pd

from geneharmony import AGRDataset, Annotator, TaxonField, resolve_taxon, taxon_mapper
from geneharmony.normalizer import build_gene_index, read_gene_index, save_gene_index
from geneharmony.store import read_metadata, read_parquet, write_parquet

RECORDS = pd.DataFrame(
    {
        "GeneId": ["HGNC:11998", "MGI:98834", "HGNC:1100"],
        "GeneSymbol": ["TP53", "Trp53", "BRCA1"],
        "GeneSynonyms": ["p53|LFS1", "p53", None],
        "GeneSystematicName": [None, None, None],
        "GeneSecondaryIds": [None, "MGI:0000001", None],
        "GeneCrossReferences": ["NCBI_Gene:7157|PANTHER:PTHR11447", "NCBI_Gene:22059", None],
        "Taxon": ["NCBITaxon:9606", "NCBITaxon:10090", "NCBITaxon:9606"],
    },
    dtype=str,
)


def main() -> None:
    assert resolve_taxon("mouse").id == "NCBITaxon:10090"
    assert taxon_mapper(TaxonField.COMMON_NAME)("NCBITaxon:9606") == "human"
    assert f"{AGRDataset.PHENOTYPES}.parquet" == "phenotypes.parquet"

    index = build_gene_index(RECORDS)
    df = index.lookup(["TP53", "p53", "NCBI_Gene:22059", "PANTHER:PTHR11447", "nope"])
    assert df["GeneId"].tolist()[:3] == ["HGNC:11998", "HGNC:11998", "MGI:98834"], df
    assert df["match_kind"].tolist()[:3] == ["OFFICIAL_SYMBOL", "SYNONYM", "CROSS_REFERENCE"]
    assert df["match_kind"].iloc[3:].isna().all(), "excluded xref and miss must not match"

    mouse = index.lookup("p53", taxon="mouse", limit=None)
    assert mouse["GeneId"].tolist() == ["MGI:98834"], mouse
    assert index.lookup("tp53", case_insensitive=True)["GeneId"].iloc[0] == "HGNC:11998"

    with tempfile.TemporaryDirectory() as tmp:
        cache = Path(tmp)
        path = cache / "gene_index.parquet"
        save_gene_index(index, path, {"geneharmony_version": "test"})
        assert read_metadata(path) == {"geneharmony_version": "test"}
        assert read_gene_index(RECORDS, path).lookup(["BRCA1"]).equals(index.lookup(["BRCA1"]))

        write_parquet(RECORDS, cache / "records.parquet", metadata={"agr_release": "1.0"})
        assert read_parquet(cache / "records.parquet").equals(RECORDS)

        with Annotator(cache) as ann:
            ann._index = index
            assert ann.normalize("BRCA1")["GeneId"].iloc[0] == "HGNC:1100"

    print("smoke test passed")


if __name__ == "__main__":
    main()
