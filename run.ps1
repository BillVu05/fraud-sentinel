<#
    fraud-sentinel task runner.

    A Makefile would be conventional, but `make` is not installed on this
    machine and PowerShell is. One file, no new tooling.

    Usage:  .\run.ps1 <task> [-N 1000] [-Id <transaction_id>]
    Tasks:  setup test train build deploy replay report narrate destroy cost
#>

param(
    [Parameter(Position = 0)][string]$Task = "help",
    [int]$N = 1000,
    [double]$Rate = 50,
    [string]$Id = "",
    [string]$Tag = "latest"
)

$ErrorActionPreference = "Stop"
$Region = "ap-southeast-2"
$Root = $PSScriptRoot

function Invoke-Step($Name, $Block) {
    Write-Host ""
    Write-Host "==> $Name" -ForegroundColor Cyan
    & $Block
    if ($LASTEXITCODE -ne 0 -and $null -ne $LASTEXITCODE) { throw "$Name failed (exit $LASTEXITCODE)" }
}

function Get-Output($Name) {
    terraform -chdir="$Root/infra" output -raw $Name
}

switch ($Task) {

    "setup" {
        Invoke-Step "install python deps" { python -m pip install -r "$Root/requirements-dev.txt" }
        Write-Host ""
        Write-Host "Still needed, once:" -ForegroundColor Yellow
        Write-Host "  winget install Amazon.AWSCLI Hashicorp.Terraform"
        Write-Host "  aws configure            # region: $Region"
        Write-Host "  Download IEEE-CIS train_transaction.csv into data/"
        Write-Host "  https://www.kaggle.com/competitions/ieee-fraud-detection/data"
    }

    "test" {
        Invoke-Step "pytest" { python -m pytest "$Root/tests" -q }
        Invoke-Step "ruff" { python -m ruff check "$Root/src" "$Root/governance" }
    }

    "train" {
        # Parity test first: if the feature contract is broken there is no point
        # spending 20 minutes training a model against it.
        Invoke-Step "parity check" { python -m pytest "$Root/tests/test_parity.py" -q }
        Invoke-Step "train" { python "$Root/src/train.py" --data "$Root/data/train_transaction.csv" }
    }

    "build" {
        if (-not (Test-Path "$Root/artifacts/model.json")) { throw "No model. Run: .\run.ps1 train" }
        $repo = Get-Output "ecr_repository_url"
        $registry = $repo.Split("/")[0]
        Invoke-Step "ecr login" {
            aws ecr get-login-password --region $Region | docker login --username AWS --password-stdin $registry
        }
        Invoke-Step "docker build" { docker build -t "${repo}:$Tag" $Root }
        Invoke-Step "docker push" { docker push "${repo}:$Tag" }
    }

    "deploy" {
        Invoke-Step "terraform init" { terraform -chdir="$Root/infra" init -upgrade }
        # Bootstrap: ECR must exist before the image can be pushed, and the
        # Lambda cannot be created until the image exists. Targeted apply first.
        Invoke-Step "terraform apply (ecr only)" {
            terraform -chdir="$Root/infra" apply -auto-approve -target=aws_ecr_repository.scorer
        }
        & "$Root/run.ps1" build -Tag $Tag
        $threshold = "0.9"
        if (Test-Path "$Root/artifacts/metrics.json") {
            $threshold = (Get-Content "$Root/artifacts/metrics.json" | ConvertFrom-Json).model.threshold
        }
        Invoke-Step "terraform apply (full)" {
            terraform -chdir="$Root/infra" apply -auto-approve `
                -var="image_tag=$Tag" -var="alert_threshold=$threshold"
        }
        # Force a new container so the updated image is actually picked up.
        $fn = "fraud-sentinel-scorer"
        $repo = Get-Output "ecr_repository_url"
        Invoke-Step "refresh lambda image" {
            aws lambda update-function-code --function-name $fn --image-uri "${repo}:$Tag" --region $Region | Out-Null
        }
        Write-Host ""
        Write-Host "Deployed. Alert threshold: $threshold" -ForegroundColor Green
        Write-Host "REMEMBER: .\run.ps1 destroy when you finish. Kinesis bills ~`$0.36/day." -ForegroundColor Yellow
    }

    "replay" {
        $stream = Get-Output "stream_name"
        Invoke-Step "replay $N records -> $stream" {
            python "$Root/src/replay.py" --stream $stream --n $N --rate $Rate --data "$Root/data/train_transaction.csv"
        }
    }

    "report" {
        $bucket = Get-Output "decision_bucket"
        New-Item -ItemType Directory -Force -Path "$Root/artifacts/decisions" | Out-Null
        Invoke-Step "pull decision log from s3" {
            aws s3 sync "s3://$bucket/decisions" "$Root/artifacts/decisions" --region $Region
        }
        Get-ChildItem -Recurse "$Root/artifacts/decisions" -Filter *.jsonl |
            Get-Content | Set-Content "$Root/artifacts/decisions.jsonl" -Encoding utf8
        Invoke-Step "latency p50/p95 (CloudWatch)" {
            $end = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
            $start = (Get-Date).ToUniversalTime().AddHours(-3).ToString("yyyy-MM-ddTHH:mm:ssZ")
            aws cloudwatch get-metric-statistics --namespace FraudSentinel --metric-name PerTransactionMs `
                --start-time $start --end-time $end --period 3600 `
                --statistics Average --extended-statistics p50 p95 --region $Region --output table
        }
        Invoke-Step "governance checks" { python "$Root/governance/checks.py" --decisions "$Root/artifacts/decisions.jsonl" }
    }

    "narrate" {
        if (-not $Id) { throw "Pass a transaction id: .\run.ps1 narrate -Id 3577209" }
        $match = Select-String -Path "$Root/artifacts/decisions.jsonl" -Pattern "`"transaction_id`": $Id[,}]" | Select-Object -First 1
        if (-not $match) { throw "Transaction $Id not in artifacts/decisions.jsonl. Run: .\run.ps1 report" }
        $match.Line | Set-Content "$Root/artifacts/sample_decision.json" -Encoding utf8
        Invoke-Step "bedrock case note" { python "$Root/src/narrate.py" --decision "$Root/artifacts/sample_decision.json" }
    }

    "destroy" {
        Invoke-Step "terraform destroy" { terraform -chdir="$Root/infra" destroy -auto-approve }
    }

    "cost" {
        $start = (Get-Date -Day 1).ToString("yyyy-MM-dd")
        $end = (Get-Date).AddDays(1).ToString("yyyy-MM-dd")
        aws ce get-cost-and-usage --time-period "Start=$start,End=$end" --granularity MONTHLY `
            --metrics UnblendedCost --group-by Type=DIMENSION,Key=SERVICE --region us-east-1 --output table
    }

    default {
        Write-Host @"
fraud-sentinel

  .\run.ps1 setup              install python deps, print the manual steps
  .\run.ps1 test               parity test + lint
  .\run.ps1 train              time-split, train, score vs baselines
  .\run.ps1 deploy             terraform apply + build/push image
  .\run.ps1 replay -N 1000     push holdout transactions into Kinesis
  .\run.ps1 report             pull decisions, latency p50/p95, governance checks
  .\run.ps1 narrate -Id <txn>  Bedrock case note for one alert
  .\run.ps1 cost               month-to-date spend by service
  .\run.ps1 destroy            tear everything down (do this every session)
"@
    }
}
