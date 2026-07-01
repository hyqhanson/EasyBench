---
title: "MELD preprocessing"
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

## Introduction
MELD is a python tool, so we prepare the input data that we need here and then export it. 


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
library(here)
```

```
## here() starts at D:/HYQ/EasyBench/benchmark_code/integration_e2e_test/autoinhibitory-feedback-preserves-intestinal-stem-cell-s44318-026-00808-x/Supp_Redhai_Hirschmueller_2024
```

``` r
source(here("plot_theme.R"))
source(here("helper_functions.R"))
```





``` r
seurat <- readRDS(here("output", "Ctrl_NotchKO_integrated_scent.rds"))
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
ctrl_split <- SplitObject(DietSeurat(seurat[, seurat$perturbation == "ctrl"], assays = "RNA"), "orig.ident")
```

```
## Error in `SplitObject()`:
## ! could not find function "SplitObject"
```

``` r
notch_split <- SplitObject(DietSeurat(seurat[, seurat$perturbation == "notch"], assays = "RNA"), "orig.ident")
```

```
## Error in `SplitObject()`:
## ! could not find function "SplitObject"
```

## Concatenate datasets
We do not do any integration here, simply merge dataset

``` r
combined_list <- c(ctrl_split, notch_split)
```

```
## Error:
## ! object 'ctrl_split' not found
```

``` r
merged_object <- merge(
    x = combined_list[[1]], y = combined_list[2:4] %>% unname(),
    project = "Notch_pertubation_merged",
    merge.data = F
)
```

```
## Error in `h()`:
## ! error in evaluating the argument 'x' in selecting a method for function 'merge': object 'combined_list' not found
```

``` r
# still no integration, just calculate PCA
merged_object <- NormalizeData(merged_object, normalization.method = "LogNormalize", scale.factor = 10000) %>%
    FindVariableFeatures(., selection.method = "vst", nfeatures = 3000) %>%
    ScaleData(.) %>%
    RunPCA(., npcs = 30, verbose = F) %>%
    RunUMAP(., reduction = "pca", dims = 1:20, verbose = F)
```

```
## Error in `RunUMAP()`:
## ! could not find function "RunUMAP"
```


After running the chunk below, we clearly see that there are strong batch effects. It looks like there is barely any overlap between the different batches (TX22&TX22_N vs TX23&TX23_N).
Thus, we split  the dataset into the different batches and then run MELD individually on them.
This was also discussed and recommended here: https://github.com/KrishnaswamyLab/MELD/issues/56 

``` r
Idents(merged_object) <- factor(merged_object$orig.ident, levels = c("TX22", "TX23", "TX22_N", "TX23_N"))
```

```
## Error:
## ! object 'merged_object' not found
```

``` r
merged_object@meta.data$test <- case_when(
    merged_object$orig.ident == "TX22" ~ "WT1",
    merged_object$orig.ident == "TX23" ~ "WT2",
    merged_object$orig.ident == "TX22_N" ~ "KO1",
    merged_object$orig.ident == "TX23_N" ~ "KO2",
)
```

```
## Error in `case_when()`:
## ! Failed to evaluate the left-hand side of formula 1.
## Caused by error:
## ! object 'merged_object' not found
```

``` r
Idents(merged_object) <- factor(merged_object$test, levels = c("WT1", "WT2", "KO1", "KO2"))
```

```
## Error:
## ! object 'merged_object' not found
```

``` r
DimPlot(merged_object, pt.size = 0.45) +
    scale_color_manual(values = color_mapping) +
    small_axis("UMAP ") +
    ggtitle("TX22 and TX23 w/o integration")
```

```
## Error in `DimPlot()`:
## ! could not find function "DimPlot"
```


Split the dataset again into the different experimentes.

``` r
tx22 <- DietSeurat(merge(x = ctrl_split$TX22, y = notch_split$TX22_N, project = "TX22"))
```

```
## Error in `DietSeurat()`:
## ! could not find function "DietSeurat"
```

``` r
tx23 <- DietSeurat(merge(x = ctrl_split$TX23, y = notch_split$TX23_N, project = "TX23"))
```

```
## Error in `DietSeurat()`:
## ! could not find function "DietSeurat"
```

``` r
# wrapper to run Normalization, Scaling, PCA and UMAP
tx22 <- run_seurat_steps(tx22, include_leiden = F, include_tsne = F)
```

```
## [1] "Running normalization and variable feature extraction"
```

```
## Error in `FindVariableFeatures()`:
## ! could not find function "FindVariableFeatures"
```

``` r
tx23 <- run_seurat_steps(tx23, include_leiden = F, include_tsne = F)
```

```
## [1] "Running normalization and variable feature extraction"
```

```
## Error in `FindVariableFeatures()`:
## ! could not find function "FindVariableFeatures"
```

``` r
####################
# EXPORT TX22 DATA #
####################
Embeddings(tx22, "pca")[, 1:20] %>%
    data.frame() %>%
    rownames_to_column("cell_barcode") %>%
    fwrite(., here("output", "MELD", "pca_data_tx22.tsv"),
        sep = "\t",
        row.names = F
    )
```

```
## Error in `Embeddings()`:
## ! could not find function "Embeddings"
```

``` r
tx22@meta.data %>%
    data.frame() %>%
    rownames_to_column("cell_barcode") %>%
    fwrite(., here("output", "MELD", "mdata_tx22.tsv"),
        sep = "\t",
        row.names = F
    )
```

```
## Error:
## ! object 'tx22' not found
```

``` r
tx22@assays$RNA@data %>%
    t() %>%
    data.frame() %>%
    rownames_to_column("cell_barcode") %>%
    fwrite(., file = here("output", "MELD", "norm_cnts_tx22.tsv"), sep = "\t", row.names = F)
```

```
## Error in `h()`:
## ! error in evaluating the argument 'x' in selecting a method for function 't': object 'tx22' not found
```

``` r
tx22@assays$RNA@counts %>%
    t() %>%
    data.frame() %>%
    rownames_to_column("cell_barcode") %>%
    fwrite(., file = here("output", "MELD", "cnts_tx22.tsv"), sep = "\t", row.names = F)
```

```
## Error in `h()`:
## ! error in evaluating the argument 'x' in selecting a method for function 't': object 'tx22' not found
```

``` r
####################
# EXPORT TX23 DATA #
####################
Embeddings(tx23, "pca")[, 1:20] %>%
    data.frame() %>%
    rownames_to_column("cell_barcode") %>%
    fwrite(., here("output", "MELD", "pca_data_tx23.tsv"),
        sep = "\t",
        row.names = F
    )
```

```
## Error in `Embeddings()`:
## ! could not find function "Embeddings"
```

``` r
tx23@meta.data %>%
    data.frame() %>%
    rownames_to_column("cell_barcode") %>%
    fwrite(., here("output", "MELD", "mdata_tx23.tsv"),
        sep = "\t",
        row.names = F
    )
```

```
## Error:
## ! object 'tx23' not found
```

``` r
tx23@assays$RNA@data %>%
    t() %>%
    data.frame() %>%
    rownames_to_column("cell_barcode") %>%
    fwrite(., file = here("output", "MELD", "norm_cnts_tx23.tsv"), sep = "\t", row.names = F)
```

```
## Error in `h()`:
## ! error in evaluating the argument 'x' in selecting a method for function 't': object 'tx23' not found
```

``` r
tx23@assays$RNA@counts %>%
    t() %>%
    data.frame() %>%
    rownames_to_column("cell_barcode") %>%
    fwrite(., file = here("output", "MELD", "cnts_tx23.tsv"), sep = "\t", row.names = F)
```

```
## Error in `h()`:
## ! error in evaluating the argument 'x' in selecting a method for function 't': object 'tx23' not found
```



``` r
sessionInfo()
```

```
## R version 4.5.2 (2025-10-31 ucrt)
## Platform: x86_64-w64-mingw32/x64
## Running under: Windows Server 2022 x64 (build 26100)
## 
## Matrix products: default
##   LAPACK version 3.12.1
## 
## locale:
## [1] LC_COLLATE=Chinese (Simplified)_China.utf8 
## [2] LC_CTYPE=Chinese (Simplified)_China.utf8   
## [3] LC_MONETARY=Chinese (Simplified)_China.utf8
## [4] LC_NUMERIC=C                               
## [5] LC_TIME=Chinese (Simplified)_China.utf8    
## 
## time zone: Asia/Shanghai
## tzcode source: internal
## 
## attached base packages:
## [1] stats4    stats     graphics  grDevices utils     datasets  methods  
## [8] base     
## 
## other attached packages:
##  [1] here_1.0.2                  patchwork_1.3.2            
##  [3] data.table_1.17.8           lubridate_1.9.5            
##  [5] forcats_1.0.1               stringr_1.6.0              
##  [7] dplyr_1.2.1                 purrr_1.2.2                
##  [9] readr_2.2.0                 tidyr_1.3.2                
## [11] tibble_3.3.1                ggplot2_4.0.3              
## [13] tidyverse_2.0.0             SingleCellExperiment_1.32.0
## [15] SummarizedExperiment_1.40.0 Biobase_2.70.0             
## [17] GenomicRanges_1.62.1        Seqinfo_1.0.0              
## [19] IRanges_2.44.0              S4Vectors_0.48.1           
## [21] BiocGenerics_0.56.0         generics_0.1.4             
## [23] MatrixGenerics_1.22.0       matrixStats_1.5.0          
## 
## loaded via a namespace (and not attached):
##  [1] SparseArray_1.10.10 stringi_1.8.7       lattice_0.22-9     
##  [4] hms_1.1.4           magrittr_2.0.5      evaluate_1.0.5     
##  [7] grid_4.5.2          timechange_0.4.0    RColorBrewer_1.1-3 
## [10] rprojroot_2.1.1     Matrix_1.7-5        scales_1.4.0       
## [13] abind_1.4-8         cli_3.6.6           rlang_1.2.0        
## [16] XVector_0.50.0      withr_3.0.3         DelayedArray_0.36.1
## [19] otel_0.2.0          S4Arrays_1.10.1     tools_4.5.2        
## [22] tzdb_0.5.0          vctrs_0.7.3         R6_2.6.1           
## [25] lifecycle_1.0.5     pkgconfig_2.0.3     pillar_1.11.1      
## [28] gtable_0.3.6        glue_1.8.1          xfun_0.59          
## [31] tidyselect_1.2.1    knitr_1.51          farver_2.1.2       
## [34] compiler_4.5.2      S7_0.2.2
```












