---
title: "MELD Evaluation"
author: "Nick Hirschmüller"
date: "01 七月, 2026"
always_allow_html: yes
output:
  html_document:
    word_document:
    toc: yes
    toc_depth: '3'
    code_folding: hide
  pdf_document:
    number_sections: yes
    toc: false
    toc_depth: 3
editor_options: 
  chunk_output_type: console
---



#Introduction
After having run the MELD python tool, we load the results back to R and create a new seurat object which contains the predicted MELD and VFC output.


``` r
library(Seurat)
```

```
## Warning: package 'Seurat' was built under R version 4.5.3
```

```
## Error:
## ! package 'SeuratObject' required by 'Seurat' could not be found
```

``` r
library(SingleCellExperiment)
```

```
## Loading required package: SummarizedExperiment
```

```
## Loading required package: MatrixGenerics
```

```
## Loading required package: matrixStats
```

```
## 
## Attaching package: 'MatrixGenerics'
```

```
## The following objects are masked from 'package:matrixStats':
## 
##     colAlls, colAnyNAs, colAnys, colAvgsPerRowSet, colCollapse,
##     colCounts, colCummaxs, colCummins, colCumprods, colCumsums,
##     colDiffs, colIQRDiffs, colIQRs, colLogSumExps, colMadDiffs,
##     colMads, colMaxs, colMeans2, colMedians, colMins, colOrderStats,
##     colProds, colQuantiles, colRanges, colRanks, colSdDiffs, colSds,
##     colSums2, colTabulates, colVarDiffs, colVars, colWeightedMads,
##     colWeightedMeans, colWeightedMedians, colWeightedSds,
##     colWeightedVars, rowAlls, rowAnyNAs, rowAnys, rowAvgsPerColSet,
##     rowCollapse, rowCounts, rowCummaxs, rowCummins, rowCumprods,
##     rowCumsums, rowDiffs, rowIQRDiffs, rowIQRs, rowLogSumExps,
##     rowMadDiffs, rowMads, rowMaxs, rowMeans2, rowMedians, rowMins,
##     rowOrderStats, rowProds, rowQuantiles, rowRanges, rowRanks,
##     rowSdDiffs, rowSds, rowSums2, rowTabulates, rowVarDiffs, rowVars,
##     rowWeightedMads, rowWeightedMeans, rowWeightedMedians,
##     rowWeightedSds, rowWeightedVars
```

```
## Loading required package: GenomicRanges
```

```
## Loading required package: stats4
```

```
## Loading required package: BiocGenerics
```

```
## Loading required package: generics
```

```
## 
## Attaching package: 'generics'
```

```
## The following objects are masked from 'package:base':
## 
##     as.difftime, as.factor, as.ordered, intersect, is.element, setdiff,
##     setequal, union
```

```
## 
## Attaching package: 'BiocGenerics'
```

```
## The following objects are masked from 'package:stats':
## 
##     IQR, mad, sd, var, xtabs
```

```
## The following objects are masked from 'package:base':
## 
##     anyDuplicated, aperm, append, as.data.frame, basename, cbind,
##     colnames, dirname, do.call, duplicated, eval, evalq, Filter, Find,
##     get, grep, grepl, is.unsorted, lapply, Map, mapply, match, mget,
##     order, paste, pmax, pmax.int, pmin, pmin.int, Position, rank,
##     rbind, Reduce, rownames, sapply, saveRDS, table, tapply, unique,
##     unsplit, which.max, which.min
```

```
## Loading required package: S4Vectors
```

```
## Warning: package 'S4Vectors' was built under R version 4.5.3
```

```
## 
## Attaching package: 'S4Vectors'
```

```
## The following object is masked from 'package:utils':
## 
##     findMatches
```

```
## The following objects are masked from 'package:base':
## 
##     expand.grid, I, unname
```

```
## Loading required package: IRanges
```

```
## 
## Attaching package: 'IRanges'
```

```
## The following object is masked from 'package:grDevices':
## 
##     windows
```

```
## Loading required package: Seqinfo
```

```
## Loading required package: Biobase
```

```
## Warning: package 'Biobase' was built under R version 4.5.3
```

```
## Welcome to Bioconductor
## 
##     Vignettes contain introductory material; view with
##     'browseVignettes()'. To cite Bioconductor, see
##     'citation("Biobase")', and for packages 'citation("pkgname")'.
```

```
## 
## Attaching package: 'Biobase'
```

```
## The following object is masked from 'package:MatrixGenerics':
## 
##     rowMedians
```

```
## The following objects are masked from 'package:matrixStats':
## 
##     anyMissing, rowMedians
```

``` r
library(tidyverse)
```

```
## Warning: package 'tidyverse' was built under R version 4.5.3
```

```
## Warning: package 'ggplot2' was built under R version 4.5.3
```

```
## Warning: package 'readr' was built under R version 4.5.3
```

```
## Warning: package 'purrr' was built under R version 4.5.3
```

```
## Warning: package 'dplyr' was built under R version 4.5.3
```

```
## Warning: package 'forcats' was built under R version 4.5.3
```

```
## Warning: package 'lubridate' was built under R version 4.5.3
```

```
## ── Attaching core tidyverse packages ──────────────────────── tidyverse 2.0.0 ──
## ✔ dplyr     1.2.1     ✔ readr     2.2.0
## ✔ forcats   1.0.1     ✔ stringr   1.6.0
## ✔ ggplot2   4.0.3     ✔ tibble    3.3.1
## ✔ lubridate 1.9.5     ✔ tidyr     1.3.2
## ✔ purrr     1.2.2
```

```
## ── Conflicts ────────────────────────────────────────── tidyverse_conflicts() ──
## ✖ lubridate::%within%() masks IRanges::%within%()
## ✖ dplyr::collapse()     masks IRanges::collapse()
## ✖ dplyr::combine()      masks Biobase::combine(), BiocGenerics::combine()
## ✖ dplyr::count()        masks matrixStats::count()
## ✖ dplyr::desc()         masks IRanges::desc()
## ✖ tidyr::expand()       masks S4Vectors::expand()
## ✖ dplyr::filter()       masks stats::filter()
## ✖ dplyr::first()        masks S4Vectors::first()
## ✖ dplyr::lag()          masks stats::lag()
## ✖ ggplot2::Position()   masks BiocGenerics::Position(), base::Position()
## ✖ purrr::reduce()       masks GenomicRanges::reduce(), IRanges::reduce()
## ✖ dplyr::rename()       masks S4Vectors::rename()
## ✖ lubridate::second()   masks S4Vectors::second()
## ✖ lubridate::second<-() masks S4Vectors::second<-()
## ✖ dplyr::slice()        masks IRanges::slice()
## ℹ Use the conflicted package (<http://conflicted.r-lib.org/>) to force all conflicts to become errors
```

``` r
library(data.table)
```

```
## 
## Attaching package: 'data.table'
## 
## The following objects are masked from 'package:lubridate':
## 
##     hour, isoweek, mday, minute, month, quarter, second, wday, week,
##     yday, year
## 
## The following objects are masked from 'package:dplyr':
## 
##     between, first, last
## 
## The following object is masked from 'package:purrr':
## 
##     transpose
## 
## The following object is masked from 'package:SummarizedExperiment':
## 
##     shift
## 
## The following object is masked from 'package:GenomicRanges':
## 
##     shift
## 
## The following object is masked from 'package:IRanges':
## 
##     shift
## 
## The following objects are masked from 'package:S4Vectors':
## 
##     first, second
```

``` r
library(patchwork)

Sys.setenv(RETICULATE_PYTHON = "/g/huber/users/hirschmueller/software/miniconda3/envs/MELD_env/bin/python")
RETICULATE_PYTHON <- "/g/huber/users/hirschmueller/software/miniconda3/envs/MELD_env/bin/python"
library(reticulate)
```

```
## Warning: package 'reticulate' was built under R version 4.5.3
```

``` r
library(here)
```

```
## here() starts at D:/HYQ/EasyBench/benchmark_code/integration_e2e_test/autoinhibitory-feedback-preserves-intestinal-stem-cell-s44318-026-00808-x/Supp_Redhai_Hirschmueller_2024
```

``` r
library(phateR)
```

```
## Loading required package: Matrix
```

```
## Warning: package 'Matrix' was built under R version 4.5.3
```

```
## 
## Attaching package: 'Matrix'
## 
## The following objects are masked from 'package:tidyr':
## 
##     expand, pack, unpack
## 
## The following object is masked from 'package:S4Vectors':
## 
##     expand
```

``` r
source(here("plot_theme.R"))
source(here("helper_functions.R"))
```


### Focus on the progenitor cells

``` r
integrated <- readRDS(here("output", "Ctrl_NotchKO_integrated_scent.rds"))
```

```
## Warning in gzfile(file, "rb"): cannot open compressed file
## 'D:/HYQ/EasyBench/benchmark_code/integration_e2e_test/autoinhibitory-feedback-preserves-intestinal-stem-cell-s44318-026-00808-x/Supp_Redhai_Hirschmueller_2024/output/Ctrl_NotchKO_integrated_scent.rds',
## probable reason 'No such file or directory'
```

```
## Error in `gzfile()`:
## ! cannot open the connection
```

``` r
# first, we reintegrate all progenitor cells (ISC, EB, EEP)
progenitors <- integrated[, integrated$high_res_annotation %in% c("ISC", "EB", "EEP")]
```

```
## Error:
## ! object 'integrated' not found
```

``` r
progenitors_split <- SplitObject(DietSeurat(progenitors, assays = "RNA"), "orig.ident")
```

```
## Error in `SplitObject()`:
## ! could not find function "SplitObject"
```

``` r
progenitors_split <- lapply(progenitors_split, function(x) {
    x <- NormalizeData(x, normalization.method = "LogNormalize", scale.factor = 10000)
    x <- FindVariableFeatures(x, selection.method = "vst", nfeatures = 3000)
    x <- ScaleData(x)
    return(x)
})
```

```
## Error in `h()`:
## ! error in evaluating the argument 'X' in selecting a method for function 'lapply': object 'progenitors_split' not found
```

``` r
# select features that are repeatedly variable across datasets for integration
features <- SelectIntegrationFeatures(object.list = progenitors_split)
```

```
## Error in `SelectIntegrationFeatures()`:
## ! could not find function "SelectIntegrationFeatures"
```

``` r
# find anchors between experiments
anchors <- FindIntegrationAnchors(
    object.list = progenitors_split,
    anchor.features = features
)
```

```
## Error in `FindIntegrationAnchors()`:
## ! could not find function "FindIntegrationAnchors"
```

``` r
progenitors_integrated <- IntegrateData(
    anchorset = anchors,
    dims = 1:15
)
```

```
## Error in `IntegrateData()`:
## ! could not find function "IntegrateData"
```

``` r
DefaultAssay(progenitors_integrated) <- "integrated"
```

```
## Error:
## ! object 'progenitors_integrated' not found
```

``` r
progenitors_integrated <- ScaleData(progenitors_integrated, verbose = F) %>%
    RunPCA(., npcs = 50, verbose = F) %>%
    RunUMAP(., reduction = "pca", dims = 1:20, verbose = F) %>%
    FindNeighbors(., dims = 1:20, k.param = 10, verbose = F) %>%
    FindClusters(.,
        resolution = c(0.3, 0.4, 0.5, 0.6, 0.7, 0.8)
    )
```

```
## Error in `FindClusters()`:
## ! could not find function "FindClusters"
```

``` r
DefaultAssay(progenitors_integrated) <- "RNA"
```

```
## Error:
## ! object 'progenitors_integrated' not found
```

``` r
# load the likelihood estimates and VFC predictions.
tx22_likelihoods <- data.table::fread(here("output", "MELD", "TX22_info_progenitors.tsv"), data.table = F) %>% tibble()
```

```
## Error in `data.table::fread()`:
## ! File 'D:/HYQ/EasyBench/benchmark_code/integration_e2e_test/autoinhibitory-feedback-preserves-intestinal-stem-cell-s44318-026-00808-x/Supp_Redhai_Hirschmueller_2024/output/MELD/TX22_info_progenitors.tsv' does not exist or is non-readable. getwd()=='D:/HYQ/EasyBench/benchmark_code/integration_e2e_test/autoinhibitory-feedback-preserves-intestinal-stem-cell-s44318-026-00808-x/Supp_Redhai_Hirschmueller_2024/scRNAseq/analyses/10_MELD'
```

``` r
tx23_likelihoods <- data.table::fread(here("output", "MELD", "TX23_info_progenitors.tsv"), data.table = F) %>% tibble()
```

```
## Error in `data.table::fread()`:
## ! File 'D:/HYQ/EasyBench/benchmark_code/integration_e2e_test/autoinhibitory-feedback-preserves-intestinal-stem-cell-s44318-026-00808-x/Supp_Redhai_Hirschmueller_2024/output/MELD/TX23_info_progenitors.tsv' does not exist or is non-readable. getwd()=='D:/HYQ/EasyBench/benchmark_code/integration_e2e_test/autoinhibitory-feedback-preserves-intestinal-stem-cell-s44318-026-00808-x/Supp_Redhai_Hirschmueller_2024/scRNAseq/analyses/10_MELD'
```

``` r
likelihoods <- rbind(tx22_likelihoods, tx23_likelihoods)
```

```
## Error:
## ! object 'tx22_likelihoods' not found
```

``` r
# add the likelihood estimate and vfc prediction to the seurat object
stopifnot(all(likelihoods$Barcode %in% progenitors_integrated@meta.data$Barcode_unique))
```

```
## Error in `h()`:
## ! error in evaluating the argument 'x' in selecting a method for function '%in%': object 'likelihoods' not found
```

``` r
progenitors_integrated@meta.data <- progenitors_integrated@meta.data %>%
    left_join(likelihoods %>% dplyr::select(-orig.ident), by = c("Barcode_unique" = "Barcode"))
```

```
## Error:
## ! object 'progenitors_integrated' not found
```

``` r
rownames(progenitors_integrated@meta.data) <- progenitors_integrated$Barcode_unique
```

```
## Error:
## ! object 'progenitors_integrated' not found
```

``` r
saveRDS(progenitors_integrated, here("output", "Ctrl_NotchKO_progenitors_integrated_meld.rds"))
```

```
## Error in `h()`:
## ! error in evaluating the argument 'object' in selecting a method for function 'saveRDS': object 'progenitors_integrated' not found
```


### The other celltypes

``` r
integrated <- readRDS(here("output", "Ctrl_NotchKO_integrated_scent.rds"))
```

```
## Warning in gzfile(file, "rb"): cannot open compressed file
## 'D:/HYQ/EasyBench/benchmark_code/integration_e2e_test/autoinhibitory-feedback-preserves-intestinal-stem-cell-s44318-026-00808-x/Supp_Redhai_Hirschmueller_2024/output/Ctrl_NotchKO_integrated_scent.rds',
## probable reason 'No such file or directory'
```

```
## Error in `gzfile()`:
## ! cannot open the connection
```

``` r
# read in all the data for which we have MELD prediction
files <- list.files(here::here("output", "MELD"), pattern = "_info_", full.names = T)

meld_res <- lapply(files, function(x) {
    # skip progenitors, they are included in ISC and EB (and part of EE)
    if (grepl("progenitors", x)) {
        return()
    }
    fread(x, data.table = F)
}) %>% bind_rows()

# add them to the seurat obj
all(meld_res$Barcode %in% Cells(integrated))
```

```
## Warning: Unknown or uninitialised column: `Barcode`.
```

```
## Error in `h()`:
## ! error in evaluating the argument 'table' in selecting a method for function '%in%': could not find function "Cells"
```

``` r
integrated@meta.data <- integrated@meta.data %>%
    left_join(meld_res %>% dplyr::select(-orig.ident), by = c("Barcode_unique" = "Barcode"))
```

```
## Error:
## ! object 'integrated' not found
```

``` r
rownames(integrated@meta.data) <- integrated$Barcode_unique
```

```
## Error:
## ! object 'integrated' not found
```

``` r
# the problem is, that dECs are missing (because they dont exist for Notch)
# this makes the dataset unsuitable for trajectory inference.
saveRDS(integrated, here("output", "Ctrl_NotchKO_per_celltype_res_meld.rds"))
```

```
## Error in `h()`:
## ! error in evaluating the argument 'object' in selecting a method for function 'saveRDS': object 'integrated' not found
```














