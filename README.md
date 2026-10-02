# aws-daily-monitoring-report

A daily email of one AWS account's health — active CloudWatch alarms, the 30-day cost
trend, Lambda errors and throttles, EventBridge rule activity, RDS load — and the same
data as a JSON snapshot in S3, if you want a dashboard to read it.

One scheduled Lambda, one IAM role, one EventBridge schedule. Deploy one stack per
account (or per account and stage); each reports on the account and region it runs in.

```bash
sam build
sam deploy --guided \
  --parameter-overrides RecipientEmail=ops@example.com SenderEmail=reports@example.com
```

## What the report covers

| Section            | What it checks                                                               |
| ------------------ | ---------------------------------------------------------------------------- |
| CloudWatch alarms  | Every alarm in `ALARM`, with metric and since when                           |
| Cost summary       | Latest complete day against the 30-day average, top 10 services, daily trend |
| Lambda health      | Errors, throttles, invocation spikes (> 10K in 24 h), peak concurrency       |
| EventBridge health | Invocations per rule on `EventBusName` over 24 h                             |
| RDS health         | CPU, free storage and peak connections per instance                          |
| Action items       | Rules over the above: cost spikes, CPU > 90 %, storage < 5 GB, throttles     |

"Latest complete day" is deliberate: Cost Explorer's end date is exclusive, so the
newest figure is yesterday's, not today's partial one.

### Thresholds

| Metric                   | Warning       | Critical                  |
| ------------------------ | ------------- | ------------------------- |
| Latest day vs 30-day avg | > 30 % above  | > 100 % above             |
| Lambda errors            | Any           | > 5 functions with errors |
| Lambda throttles         | —             | Any                       |
| Lambda invocations       | > 10K in 24 h | —                         |
| RDS CPU                  | > 70 % max    | > 90 % max                |
| RDS free storage         | —             | < 5 GB                    |

## Before you deploy

**Verify both addresses in SES**, in the region you deploy to. While the account's SES
is in the sandbox, the recipient must be verified as well as the sender.

```bash
aws ses verify-email-identity --email-address reports@example.com
aws ses verify-email-identity --email-address ops@example.com
```

**Cost Explorer has to be readable.** In a member account of an AWS Organization,
`GetCostAndUsage` fails with _User not enabled for cost explorer access_ until the
management account enables member-account access to billing data. The report does not
fail when that happens: the cost section says Cost Explorer is unavailable and raises
a warning, and everything else is still sent.

## Parameters

| Parameter            | Default              | What it is                                                                        |
| -------------------- | -------------------- | --------------------------------------------------------------------------------- |
| `RecipientEmail`     | — (required)         | SES "To" address                                                                  |
| `SenderEmail`        | — (required)         | SES "From" address; a verified identity                                           |
| `AccountName`        | empty                | Name in the report title. Empty uses the IAM account alias, then the account id   |
| `FunctionNameSuffix` | empty                | Appended to the function name — only for two stacks in one account and region     |
| `ScheduleExpression` | `cron(0 10 * * ? *)` | When the report runs: 10:00 UTC daily                                             |
| `ScheduleEnabled`    | `true`               | `false` keeps the function but stops the schedule                                 |
| `MonitoringRegion`   | empty                | The region reported on. Empty means the stack's own                               |
| `EventBusName`       | `default`            | The bus whose rules are sampled. A missing bus is reported, not fatal             |
| `SnapshotBucket`     | empty                | An existing bucket to publish the snapshot to. Empty: email only                  |
| `SnapshotPrefix`     | `snapshots/`         | Key prefix in that bucket; ends with `/`                                          |
| `SnapshotSlug`       | empty                | The snapshot's name, `<prefix><slug>.json`. Required when `SnapshotBucket` is set |

The function is named `aws-daily-monitoring-report` — a fixed name, so it is easy to
find and invoke. That is also why two stacks in one account and region need
`FunctionNameSuffix`, e.g. `-dev` and `-production`.

## The snapshot

With `SnapshotBucket` set, every run also writes `<SnapshotPrefix><SnapshotSlug>.json`:
the same data as the email, in a stable shape (`"schema": 1`). It is attached to the
email too, and printed at its foot, so a report can be pasted into whatever reads it.

- **The slug is required, and never guessed.** Without one the function refuses to
  write rather than falling back to the account id: two stacks in one account would
  otherwise overwrite each other, and a dashboard keyed by name would never find it.
- **The bucket is not created here.** In the same account, the function's own policy is
  enough. In **another** account, that bucket's policy must grant this account
  `s3:PutObject` and `s3:PutObjectAcl` on the prefix first. Until it does, every write
  fails with 403 and the email still arrives — which is why the invoke result reports
  `snapshotWritten` separately.
- The object is written with `bucket-owner-full-control`, so a bucket owner in another
  account owns it and its policy applies.

## Deploying

### From source

```bash
sam build
sam deploy --guided \
  --parameter-overrides RecipientEmail=ops@example.com SenderEmail=reports@example.com
```

or `make deploy RECIPIENT=ops@example.com SENDER=reports@example.com`, which passes
any other parameters through `PARAMS="AccountName=Production"`. The stack needs
`CAPABILITY_IAM` and nothing else: its one role has a generated name.

### Through a CloudFormation service role

If your CI may only drive CloudFormation and passes a service role for the resources —
the split [aws-account-bootstrap](https://github.com/jnet-platform-factory/aws-account-bootstrap)
sets up with `app-deploy-role` and `app-cfn-exec-role` — add the role:

```bash
sam deploy --role-arn arn:aws:iam::123456789012:role/app-cfn-exec-role \
  --capabilities CAPABILITY_IAM --parameter-overrides …
```

That role needs to create a Lambda function, an IAM role with an inline policy, an
EventBridge rule and a log group — nothing more.

### From the Serverless Application Repository

The template carries SAR metadata (`aws-daily-monitoring-report`, `SemanticVersion:
1.0.0`), and `make release S3_BUCKET=…` gates, packages and publishes it. **1.0.0 has
not been published yet**; until it is, deploy from source. Once a listing exists and is
shared with your account:

```yaml
Resources:
  DailyMonitoringReport:
    Type: AWS::Serverless::Application
    Properties:
      Location:
        ApplicationId: arn:aws:serverlessrepo:us-east-1:123456789012:applications/aws-daily-monitoring-report
        SemanticVersion: 1.0.0
      Parameters:
        RecipientEmail: ops@example.com
        SenderEmail: reports@example.com
```

A SAR application exists only in the region it was published to, and Lambda needs its
code in the function's region — so a deployment in another region needs a listing
there.

## Checking it works

A stack in `CREATE_COMPLETE` has proved nothing about delivery. Send a report now:

```bash
make invoke
# or
aws lambda invoke --function-name aws-daily-monitoring-report \
  --payload '{}' --cli-binary-format raw-in-base64-out /dev/stdout
```

Expect `"statusCode": 200` and the email within a minute. `"snapshotWritten": true`
is the only proof the snapshot landed — `false` with a bucket set means a 403, a
missing slug, or a bucket that does not exist; `make logs` says which.

## Permissions

The function is granted read-only access, plus sending email and the snapshot write:

- `cloudwatch:DescribeAlarms`, `GetMetricStatistics`, `ListMetrics`
- `ce:GetCostAndUsage`
- `lambda:ListFunctions`, `rds:DescribeDBInstances`, `events:ListRules`
- `sts:GetCallerIdentity`, `iam:ListAccountAliases`
- `ses:SendEmail`, `ses:SendRawEmail`
- `s3:PutObject`, `s3:PutObjectAcl` on `<SnapshotBucket>/<SnapshotPrefix>*` — only
  when `SnapshotBucket` is set

## Developing

```bash
make check     # sam validate --lint, tests, leak check, SAR metadata — no AWS needed
make help      # everything else
```

**This repository is public.** `make leak-check` scans the whole tree and refuses any
12-digit account id other than the documentation placeholders (`123456789012`,
`111122223333`), any email address outside `example.com`, credentials and host CIDRs.
Organisation-specific terms go in a git-ignored `.leak-check-deny`, or the
`LEAK_CHECK_DENY` CI secret — never in the repository, because a list of what must not
be published is itself something that must not be published.

## Removing

```bash
sam delete --stack-name aws-daily-monitoring-report
```

The log group `/aws/lambda/aws-daily-monitoring-report` is created by Lambda, not by
the stack, so it survives the delete. Remove it yourself if you do not want the
history.

## License

[Apache-2.0](LICENSE.txt)
