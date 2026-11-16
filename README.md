# geneharmony

An async Python toolkit for normalizing gene identifiers and annotating gene sets with data from the [Alliance of Genome Resources](https://www.alliancegenome.org) (AGR) REST API and bulk-download files. 
You can also join your own annotation tables onto the results.

Gene symbols and identifiers are resolved to canonical AGR genes using an in-memory index built from AGR's bulk gene file. Per-gene API data is fetched concurrently, and bulk datasets are downloaded and parsed.

## Features

- **Gene normalization**: Resolve symbols, primary and secondary IDs, synonyms, and external cross-references (NCBI, Ensembl, UniProtKB, RefSeq, etc.) to records in AGR's `GENE-TSV-COMBINED` file.
- **Nine model organisms**: Human, mouse, rat, zebrafish, fly, worm, yeast, African clawed frog, and western clawed frog.
- **Concurrent requests**: Pooled, rate-limited HTTP requests with automatic retries for transient failures.
- **Caching**: Bulk files, per-gene API results, and ingested annotations are cached as Parquet files.
- **Custom annotations**: Ingest your own annotation tables keyed on any supported gene identifier and join them with AGR data.

## Installation

Requires Python 3.12+.

```bash
pip install geneharmony
# or
uv add geneharmony
```

## Usage

The functions of the `Annotator` class are async and must be called with `await`.

### Quick start

```python
from geneharmony import Annotator, AGRDataset

ann = Annotator()

# Resolve gene symbols to canonical AGR records
genes = await ann.normalize(["TP53", "BRCA1"], taxon="human")

# Annotate genes with phenotypes
annotated_genes = await ann.annotate(["Atp7b", "Ttn"], AGRDataset.PHENOTYPES, taxon="mouse")
```

### Resolving genes

`normalize` takes one identifier or a list of identifiers and returns one row per match. Queries with no match are kept in the output with a null `match_kind`.

```python
df = await ann.normalize(
    ["TP53", "ENSG00000141510", "not_a_gene"],
    taxon="human",          # "human", "9606", "Homo sapiens", or "NCBITaxon:9606"
    limit=1,                # max matches per query; None returns all matches
    case_insensitive=False, # case matters across species (human TP53 vs mouse Trp53)
)

resolved = df[df.match_kind.notna()]
```

Matches are ranked by the type of identifier they matched on:

```
PRIMARY_ID > SECONDARY_ID > OFFICIAL_SYMBOL > SYNONYM > CROSS_REFERENCE
```

### Annotating genes

`annotate` normalizes the input genes, then left joins each source onto the canonical `GeneId` in the order given. A source is either an `AGRDataset` or the name of a table added with [`ingest_annotation`](#adding-your-own-annotations). You can pass any number of sources at once.

```python
# An AGR dataset
orth = await ann.annotate(["TP53", "BRCA1"], AGRDataset.ORTHOLOGY, taxon="human")

# Several sources in one call
df = await ann.annotate(["TP53", "BRCA1"], AGRDataset.ORTHOLOGY, "expression", taxon="human")

# Only an ingested annotation
df = await ann.annotate(["TP53", "BRCA1"], "expression", taxon="human")
```

AGR dataset columns keep their original names, and columns from an ingested table are prefixed with `<name>.`. Each source also adds a boolean `has.<source>` column that marks which rows matched that source.

You can also pass a DataFrame with a `GeneId` column, such as the output of `normalize`, instead of a list of genes.

Most sources have several rows per gene, so each source you add to a call multiplies the number of rows. To keep the output small, filter the result of one call and pass the genes you want to the next:

```python
# Find orthologs of human genes
orth = await ann.annotate(["TP53", "BRCA1"], AGRDataset.ORTHOLOGY, taxon="human")

# Keep the mouse orthologs
mouse = orth.loc[orth.Gene2SpeciesTaxonID == "NCBITaxon:10090", "Gene2ID"].unique()

# Get the phenotypes of the mouse genes
pheno = await ann.annotate(list(mouse), AGRDataset.PHENOTYPES, taxon="mouse")
```

#### Available datasets

| Dataset                 | Source       | Main columns                                              |
| ----------------------- | ------------ | --------------------------------------------------------- |
| `AGRDataset.ORTHOLOGY`  | Bulk TSV     | `Gene2ID`, `Gene2Symbol`, `Gene2SpeciesTaxonID`, ...      |
| `AGRDataset.PHENOTYPES` | Per-gene API | `phenotypeStatement`, `references`                        |
| `AGRDataset.ALLELES`    | Per-gene API | `allele_id`, `symbol`, `alterationType`, `variantType`, ... |

### Orthologs

`get_orthologs` returns a smaller set of ortholog columns:

```python
orthologs = await ann.get_orthologs(
    ["TP53", "BRCA1"],
    taxon="human",
    target_taxon="mouse",   # optional
)
# columns: query, match_kind, Gene2ID, Gene2Symbol, Gene2SpeciesTaxonID
```

### Downloading bulk datasets

Bulk datasets are downloaded and converted to Parquet the first time they are used. To download one ahead of time:

```python
path = await ann.download(AGRDataset.ORTHOLOGY)

# Re-download after a new AGR release
path = await ann.download(AGRDataset.ORTHOLOGY, refresh=True)
```

### Adding your own annotations

`ingest_annotation` takes a CSV, TSV, or Parquet file (or a `DataFrame`), normalizes its gene identifiers, and stores it under a name you can pass to `annotate`.

```python
summary, unmapped = await ann.ingest_annotation(
    "my_expression_table.csv",
    name="expression",
    gene_id_column="symbol",   # or a list of columns, tried in order for each row
    taxon="human",
)

# Columns from the ingested table are prefixed with "expression."
df = await ann.annotate(["TP53", "BRCA1"], AGRDataset.ORTHOLOGY, "expression", taxon="human")
```

`summary` contains the number of rows read, stored, and dropped. `unmapped` contains the rows whose identifiers could not be resolved.

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

The `taxon` argument accepts the common name, species name, taxon number, or full `NCBITaxon:` ID.

## Caching

The cache is stored in `$XDG_CACHE_HOME/geneharmony`, or `~/.cache/geneharmony` if `XDG_CACHE_HOME` is not set. Pass `cache_dir` to `Annotator` to use a different location.

```
bulk/<dataset>.parquet            # bulk datasets, including the gene index source
api/<dataset>/<gene_id>.parquet   # per-gene API results
external/<name>.parquet           # ingested annotations
```

The gene index is built from `bulk/gene.parquet`. To update it after a new AGR release, run `await ann.download(AGRDataset.GENE, refresh=True)` or delete the file.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## Acknowledgements and citation

This project uses data and services provided by the Alliance of Genome Resources (AGR). It is not affiliated with or endorsed by the Alliance. All gene, ortholog, phenotype, and allele data come from AGR and its member model organism databases, and are subject to the Alliance's terms of use.

If you use data obtained through geneharmony, please cite the Alliance of Genome Resources:

> [Updates to the Alliance of Genome Resources central infrastructure.](https://pubmed.ncbi.nlm.nih.gov/38552170/) 2024. Alliance of Genome Resources Consortium. Genetics. 2024 May 7;227(1):iyae049. doi: 10.1093/genetics/iyae049. PMID: 38552170.

See the [Alliance citation guidelines](https://www.alliancegenome.org/cite-us) and cite the relevant model organism databases (SGD, WormBase, FlyBase, ZFIN, MGI, RGD, Xenbase) where appropriate.
