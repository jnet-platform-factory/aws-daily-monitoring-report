# ---------------------------------------------------------------------------
# aws-daily-monitoring-report — validate / test / build / deploy / publish
#
#   make check                                  # everything that needs no AWS
#   make deploy RECIPIENT=ops@example.com SENDER=reports@example.com
#   make invoke                                 # send a report now
#   make release S3_BUCKET=my-sar-artifacts     # gates, then publish to SAR
#
# Auth: export AWS_PROFILE, or set AWS_VAULT=<profile> to wrap every AWS command
# in `aws-vault exec`.
# ---------------------------------------------------------------------------

APP_NAME       := aws-daily-monitoring-report
REGION         ?= us-east-1
STACK_NAME     ?= aws-daily-monitoring-report
FUNCTION_NAME  ?= aws-daily-monitoring-report
S3_BUCKET      ?=
ROLE_ARN       ?=
TEMPLATE       := template.yaml
PACKAGED       := packaged.yaml
BUILT_TEMPLATE := .aws-sam/build/template.yaml
PYTHON         ?= python3

RECIPIENT ?=
SENDER    ?=
# Any other parameters, as Key=Value pairs: PARAMS="AccountName=Production SnapshotBucket=..."
PARAMS    ?=

AWS_VAULT ?=
ifeq ($(AWS_VAULT),)
RUN :=
else
RUN := aws-vault exec $(AWS_VAULT) --
endif

# Deploy through a CloudFormation service role when one is given, e.g.
# ROLE_ARN=arn:aws:iam::123456789012:role/app-cfn-exec-role
ifeq ($(ROLE_ARN),)
ROLE_FLAG :=
else
ROLE_FLAG := --role-arn $(ROLE_ARN)
endif

.DEFAULT_GOAL := help

.PHONY: help
help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'
	@echo ""
	@echo "Vars: REGION=$(REGION) STACK_NAME=$(STACK_NAME) S3_BUCKET=$(S3_BUCKET) ROLE_ARN=$(ROLE_ARN) AWS_VAULT=$(AWS_VAULT)"

guard-%:
	@if [ -z "$($*)" ]; then \
		echo "ERROR: '$*' is required, e.g. make $(MAKECMDGOALS) $*=<value>"; \
		exit 1; \
	fi

# ---------------------------------------------------------------------------
# Gates — none of this needs AWS credentials
# ---------------------------------------------------------------------------
.PHONY: validate
validate: ## Lint & validate the SAM template
	sam validate --lint --region $(REGION)

.PHONY: test
test: ## Run the unit tests
	$(PYTHON) -m pytest tests/ -q

.PHONY: leak-check
leak-check: ## Fail if anything internal is in the tree (see scripts/leak-check.py)
	$(PYTHON) scripts/leak-check.py

.PHONY: check-metadata
check-metadata: ## Fail on SAR metadata sam publish would reject
	$(PYTHON) scripts/check-sar-metadata.py

.PHONY: check
check: validate test leak-check check-metadata ## All of the above

# ---------------------------------------------------------------------------
# Build and deploy into your own account
# ---------------------------------------------------------------------------
.PHONY: build
build: ## Build the function
	sam build

.PHONY: check-build
check-build: ## Fail if the built artifact is incomplete
	$(PYTHON) scripts/check-build.py

.PHONY: deploy
deploy: guard-RECIPIENT guard-SENDER build ## Deploy into the current account (RECIPIENT, SENDER required)
	$(RUN) sam deploy --template-file $(BUILT_TEMPLATE) --stack-name $(STACK_NAME) --region $(REGION) \
		--capabilities CAPABILITY_IAM --resolve-s3 --no-fail-on-empty-changeset $(ROLE_FLAG) \
		--parameter-overrides RecipientEmail=$(RECIPIENT) SenderEmail=$(SENDER) $(PARAMS)

.PHONY: invoke
invoke: ## Send a report now, and show whether the snapshot was written
	$(RUN) aws lambda invoke --function-name $(FUNCTION_NAME) --region $(REGION) \
		--payload '{}' --cli-binary-format raw-in-base64-out /dev/stdout

.PHONY: logs
logs: ## Tail the function's logs
	$(RUN) aws logs tail /aws/lambda/$(FUNCTION_NAME) --region $(REGION) --follow

.PHONY: destroy
destroy: ## Delete the stack
	$(RUN) sam delete --stack-name $(STACK_NAME) --region $(REGION)

# ---------------------------------------------------------------------------
# Publish to the Serverless Application Repository
# ---------------------------------------------------------------------------
.PHONY: package
package: guard-S3_BUCKET check-build ## Upload the BUILT artifact to S3 and emit packaged.yaml
	# BUILT_TEMPLATE, not TEMPLATE: packaging the source template uploads app/
	# without whatever `sam build` installed. See scripts/check-build.py.
	$(RUN) sam package --template-file $(BUILT_TEMPLATE) --output-template-file $(PACKAGED) \
		--s3-bucket $(S3_BUCKET) --region $(REGION)

.PHONY: publish
publish: ## Publish packaged.yaml to SAR
	$(RUN) sam publish --template $(PACKAGED) --region $(REGION)
	@echo ""
	@echo "Published. SemanticVersion is IMMUTABLE — bump it in $(TEMPLATE) before"
	@echo "the next publish. A SAR application exists only in the region it was"
	@echo "published to: publish once per region that deploys it."

.PHONY: release
release: check build check-build package ## Gates, build, package, leak-check the package, publish
	$(PYTHON) scripts/leak-check.py .aws-sam/build
	$(MAKE) publish

.PHONY: clean
clean: ## Remove build artifacts
	rm -rf .aws-sam $(PACKAGED)
