<!-- Sample output, generated against moto (in-memory fake AWS) with made-up accounts and volumes. -->

# Unattached EBS volumes report

Generated 2027-01-18 23:54 UTC · volumes unattached and created at least **30 days** ago.

| Metric | Value |
|---|---|
| Accounts scanned | 3 |
| Account/region pairs scanned | 6 |
| Unattached volumes | 6 (1 protected by tag) |
| Provisioned storage | 3,850 GiB |
| Estimated cost | **$801.50 / month** · $9,618.00 / year |
| Scan errors | 0 |

> Prices for eu-west-1 are not in the price table; those volumes were priced with the fallback region. Use `--price-file` for exact figures.

## By account

| | Volumes | GiB | $/month |
|---|---:|---:|---:|
| 020877340245 prod | 3 | 1,700 | $698.50 |
| 723592890914 dev | 2 | 2,100 | $98.00 |
| 123456789012 master | 1 | 50 | $5.00 |

## By region

| | Volumes | GiB | $/month |
|---|---:|---:|---:|
| us-east-1 | 5 | 1,850 | $711.50 |
| eu-west-1 | 1 | 2,000 | $90.00 |

## By volume type

| | Volumes | GiB | $/month |
|---|---:|---:|---:|
| io2 | 1 | 500 | $582.50 |
| gp2 | 2 | 1,050 | $105.00 |
| st1 | 1 | 2,000 | $90.00 |
| gp3 | 2 | 300 | $24.00 |

## Top 6 by cost

| Account | Region | Volume | Name | Type | GiB | Age (days) | $/month | Protected |
|---|---|---|---|---|---:|---:|---:|---|
| 020877340245 | us-east-1 | `vol-b7da7b4aa39d0b10d` | old-oracle-data | io2 | 500 | 95 | $582.50 |  |
| 020877340245 | us-east-1 | `vol-8a8ace61eb280bf29` | elk-hot-1 | gp2 | 1,000 | 95 | $100.00 |  |
| 723592890914 | eu-west-1 | `vol-06c113597079c3fba` | batch-scratch | st1 | 2,000 | 95 | $90.00 |  |
| 020877340245 | us-east-1 | `vol-ad5d928c5fa5e4f80` | legal-archive | gp3 | 200 | 95 | $16.00 | DoNotDelete |
| 723592890914 | us-east-1 | `vol-7c86e56d25b7c7f62` | ci-cache | gp3 | 100 | 95 | $8.00 |  |
| 123456789012 | us-east-1 | `vol-bb573ac6068bfbeb4` |  | gp2 | 50 | 95 | $5.00 |  |

---
Costs are on-demand list-price estimates for the volumes only (snapshots are not included). Age is measured from creation time: EC2 does not record when a volume was detached.
