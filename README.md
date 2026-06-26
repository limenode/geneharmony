# geneharmony

An async Python toolkit that normalizes gene identifiers and annotates gene sets using the [Alliance of Genome Resources](https://www.alliancegenome.org) (AGR) REST API and bulk-download files, with functionality to append local annotations.

It resolves gene symbols and identifiers to canonical genes using an in-memory index built from AGR's bulk gene file, fetches per-gene API data concurrently, and downloads and parses AGR bulk datasets.

## Highlights

- **Gene normalization**: Resolve symbols, primary/secondary IDs, synonyms, systematic names, and external cross-references (NCBI, Ensembl, UniProtKB, RefSeq, …) to the appropriate records in the AGR's `GENE-TSV-COMBINED` file.
- **Nine model organisms**: Human, mouse, rat, zebrafish, fly, worm, yeast, african clawed frog, and western clawed frog.
- **Concurrent and resilient**: Pooled, rate-limited HTTP with automatic retry/backoff for transient failures.
- **Transparent caching**: Bulk files, per-gene API results, and ingested annotations are cached as Parquet to expedite repeat runs.
- **Bring your own data**: Ingest external annotation tables keyed on whatever gene identifier you have; they normalize to canonical AGR genes and join cleanly.

## Requirements

- **Python 3.14**
- Dependencies (managed via [pixi](https://pixi.sh)): `httpx`, `pydantic` v2, `pandas` 3.x, `pyarrow`, `numpy`

## Setup

This project uses **pixi** (conda-forge) for environment management.

```bash
# 1. Install the environment from the lockfile
pixi install

# 2. Run Python inside the environment
pixi run python <script>

# 3. Or open the interactive driver notebook
pixi run jupyter lab src/notebook.ipynb
```

### Developer setup (optional)

Notebook outputs are stripped from version control via a git clean filter. The filter config is repo-local, so enable it once per clone:

```bash
git config filter.nbstrip.clean "pixi run jupyter nbconvert --clear-output --to notebook --stdin --stdout --log-level=ERROR"
git config filter.nbstrip.smudge cat
```

## Usage

The `Annotator` is the single entry point. It is async, so call its methods with `await` (inside a notebook cell, an `async def`, or `asyncio.run(...)`).

### Quick start

```python
from geneharmony import Annotator, AGRDataset

ann = Annotator()

# Resolve gene symbols to canonical AGR records
genes = await ann.normalize(["TP53", "BRCA1"], taxon="human")

# Annotate genes with phenotypic information
annotated_genes = await ann.annotate(["Atp7b", "Ttn"], AGRDataset.PHENOTYPES, taxon="mouse")
```

### Resolving genes (`normalize`)

`normalize` accepts an identifier or a list of identifiers and returns one row per match. Unmatched queries are **retained** with a null `match_kind` so misses stay visible.

```python
df = await ann.normalize(
    ["TP53", "ENSG00000141510", "not_a_gene"],
    taxon="human",          # any alias: "human", "9606", "Homo sapiens", "NCBITaxon:9606"
    limit=1,                # max matches per query; use None for all
    case_insensitive=False, # case can be meaningful (human TP53 vs mouse Trp53)
)

resolved = df[df.match_kind.notna()]   # drop the misses
```

Matches are ranked by identifier precedence:

```
PRIMARY_ID > SECONDARY_ID > OFFICIAL_SYMBOL > SYNONYM > CROSS_REFERENCE
```

### Annotating genes (`annotate`)

`annotate` builds a normalized base frame, then **left joins** one or more sources onto the canonical `GeneId`:

```python
from geneharmony import AGRDataset

orth = await ann.annotate(
    ["TP53", "BRCA1"],
    AGRDataset.ORTHOLOGY,
    taxon="human",
)
```

For chaining annotate calls, the recommended pattern is an **iterative filter-then-requery traversal** — one AGR dataset per call — so result cardinality stays under your control:

```python
# 1. Find orthologs of human genes
orth = await ann.annotate(["TP53", "BRCA1"], AGRDataset.ORTHOLOGY, taxon="human")

# 2. Keep the mouse orthologs
mouse = orth.loc[orth.Gene2SpeciesTaxonID == "NCBITaxon:10090", "Gene2ID"].unique()

# 3. Fetch their phenotypes
pheno = await ann.annotate(list(mouse), AGRDataset.PHENOTYPES, taxon="mouse")
```

#### Available AGR datasets

| Dataset                 | Backend      | Key columns contributed                                     |
| ----------------------- | ------------ | ----------------------------------------------------------- |
| `AGRDataset.ORTHOLOGY`  | Bulk TSV     | `Gene2ID`, `Gene2Symbol`, `Gene2SpeciesTaxonID`, …          |
| `AGRDataset.PHENOTYPES` | Per-gene API | `phenotypeStatement`, `references`                          |
| `AGRDataset.ALLELES`    | Per-gene API | `allele_id`, `symbol`, `alterationType`, `variantType`, …   |

### Orthologs convenience helper

For the common ortholog case there is a shortcut that returns a tidy subset:

```python
orthologs = await ann.get_orthologs(
    ["TP53", "BRCA1"],
    taxon="human",
    target_taxon="mouse",   # optional: filter to one target species
)
# -> columns: query, match_kind, Gene2ID, Gene2Symbol, Gene2SpeciesTaxonID
```

### Downloading bulk datasets (`download`)

Bulk datasets are downloaded and converted to Parquet on first use (and cached thereafter). You can pre-fetch one explicitly:

```python
path = await ann.download(AGRDataset.ORTHOLOGY)
# Force a refresh across AGR releases:
path = await ann.download(AGRDataset.ORTHOLOGY, refresh=True)
```

### Ingesting your own annotations (`ingest_annotation`)

Bring an external table (CSV, TSV, or Parquet file, or a `DataFrame`), normalize its gene identifiers to canonical AGR genes, and store it for joining by name:

```python
summary, unmapped = await ann.ingest_annotation(
    "my_expression_table.csv",
    name="expression",
    gene_id_column="symbol",   # or a list of columns, tried left-to-right per row
    taxon="human",
)

# Join it alongside an AGR dataset; its columns are prefixed `expression.`
df = await ann.annotate(["TP53", "BRCA1"], AGRDataset.ORTHOLOGY, "expression", taxon="human")
```

`summary` reports rows in / stored / dropped; `unmapped` holds the rows whose identifiers could not be resolved (with their original columns) so nothing is silently lost.

### Supported species

| Common name           | Species                          | Taxon ID           |
| --------------------- | -------------------------------- | ------------------ |
| human                 | *Homo sapiens*                   | `NCBITaxon:9606`   |
| mouse                 | *Mus musculus*                   | `NCBITaxon:10090`  |
| rat                   | *Rattus norvegicus*              | `NCBITaxon:10116`  |
| zebrafish             | *Danio rerio*                    | `NCBITaxon:7955`   |
| fly / fruit fly       | *Drosophila melanogaster*        | `NCBITaxon:7227`   |
| worm / roundworm      | *Caenorhabditis elegans*         | `NCBITaxon:6239`   |
| yeast / budding yeast | *Saccharomyces cerevisiae S288C* | `NCBITaxon:559292` |
| african clawed frog   | *Xenopus laevis*                 | `NCBITaxon:8355`   |
| western clawed frog   | *Xenopus tropicalis*             | `NCBITaxon:8364`   |

Any of the aliases above — common name, full species name, bare number, or `NCBITaxon:` ID — can be passed as `taxon`.

## Caching

Results are cached so repeat work is fast and largely offline. The cache defaults to `$XDG_CACHE_HOME/geneharmony` (falling back to `~/.cache/geneharmony`); pass a `cache_dir` to `Annotator(...)` to share or relocate it.

```
bulk/<dataset>.parquet            # downloaded + converted bulk datasets (incl. the gene index source)
api/<dataset>/<gene_id>.parquet   # per-gene API results
external/<name>.parquet           # ingested annotations
```

The gene index is built from `bulk/gene.parquet`, which becomes stale across AGR releases. Refresh it with `await ann.download(AGRDataset.GENE, refresh=True)` (or by deleting the file).

## Acknowledgements & Citation

This project is a client for data and services provided by the **Alliance of Genome Resources (AGR)**. It is not affiliated with or endorsed by the Alliance. All gene, ortholog, phenotype, and allele data are sourced from AGR and its member model-organism databases, and remain subject to the Alliance's terms of use.

If you use data obtained through this wrapper, please cite the Alliance of Genome Resources:

> [Updates to the Alliance of Genome Resources central infrastructure.](https://pubmed.ncbi.nlm.nih.gov/38552170/) 2024. Alliance of Genome Resources Consortium. Genetics. 2024 May 7;227(1):iyae049. doi: 10.1093/genetics/iyae049. PMID: 38552170.

Please also consult the [Alliance citation and data-usage guidelines](https://www.alliancegenome.org/cite-us) and acknowledge the underlying model-organism databases (e.g. SGD, WormBase, FlyBase, ZFIN, MGI, RGD, Xenbase) as appropriate.
