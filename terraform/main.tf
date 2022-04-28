variable "name" {
  default = "ec2-snoozable-shutdown"
}
variable "slack_bot_token" {
    default = "xoxb-xxxxx"
}

variable "slack_channel" {
  default = "test-channel"
}

provider "aws" {
  region = "ap-northeast-1"
}

resource "aws_iam_role" "role" {
  name = "${var.name}-role"

  assume_role_policy = <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Service": "lambda.amazonaws.com"
      },
      "Action": "sts:AssumeRole"
    }
  ]
}
EOF
}

# Lambda付与ポリシー
data "aws_iam_policy_document" "policy" {
  statement {
    actions = [
      "ec2:StopInstances",
      "ec2:DescribeInstances",
      "ec2:CreateTags",
      "cloudwatch:GetMetricStatistics"
    ]

    resources = [
      "*",
    ]
  }
}

resource "aws_iam_role_policy" "policy" {
  name   = "${var.name}-policy"
  role   = aws_iam_role.role.name
  policy = data.aws_iam_policy_document.policy.json
}

resource "aws_iam_role_policy_attachment" "eni" {
  role       = aws_iam_role.role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaENIManagementAccess"
}

resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}


resource "aws_lambda_function" "lambda" {
  filename      = "lambda_function.zip"
  function_name = var.name
  role          = aws_iam_role.role.arn
  handler       = "lambda_function.lambda_handler"
  memory_size   = 1024
  timeout       = 300
  runtime       = "python3.9"

  environment {
    variables = {
      SLACK_BOT_TOKEN = var.slack_bot_token
      SLACK_CHANNEL   = var.slack_channel
    }
  }
}

# CloudWatch を追加
resource "aws_cloudwatch_event_rule" "er" {
  name                = "${var.name}-event-rule"
  description         = "${var.name} event rule"
  schedule_expression = "rate(10 minutes)"
}

resource "aws_cloudwatch_event_target" "et" {
  rule      = aws_cloudwatch_event_rule.er.name
  target_id = "${var.name}-et"
  arn       = aws_lambda_function.lambda.arn
}

resource "aws_lambda_permission" "allow_cloudwatch_to_call_lambda" {
  statement_id  = "AllowExecutionFromCloudWatch"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.lambda.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.er.arn
}

# APIGatewayを作成してLambdaと繋ぐ
resource "aws_api_gateway_rest_api" "api" {
  name = "${var.name}-endopint"
}

resource "aws_api_gateway_resource" "lambda" {
  path_part   = var.name
  parent_id   = aws_api_gateway_rest_api.api.root_resource_id
  rest_api_id = aws_api_gateway_rest_api.api.id
}

# How the gateway will be interacted from clientt
resource "aws_api_gateway_method" "lambda" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.lambda.id
  http_method   = "POST"
  authorization = "NONE"
}

# Integration between lambda and terraform
resource "aws_api_gateway_integration" "redirect" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.lambda.id
  http_method = aws_api_gateway_method.lambda.http_method
  # Lambda invokes requires a POST method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.lambda.invoke_arn
}

resource "aws_api_gateway_deployment" "deployment" {
  depends_on  = [aws_api_gateway_rest_api.api]
  rest_api_id = aws_api_gateway_rest_api.api.id
  stage_name  = "dev"

  triggers = {
    redeployment = "v0.1"
  }

  lifecycle {
    create_before_destroy = true
  }
}


resource "aws_lambda_permission" "allow_apigateway_to_call_lambda" {
  statement_id  = "AllowExecutionFromAPIGateway"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.lambda.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.api.execution_arn}/*/*"
}

resource "aws_api_gateway_account" "demo" {
  cloudwatch_role_arn = aws_iam_role.cloudwatch.arn
}

resource "aws_iam_role" "cloudwatch" {
  name = "api_gateway_cloudwatch_global"

  assume_role_policy = <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "",
      "Effect": "Allow",
      "Principal": {
        "Service": "apigateway.amazonaws.com"
      },
      "Action": "sts:AssumeRole"
    }
  ]
}
EOF
}

resource "aws_iam_role_policy" "cloudwatch" {
  name = "default"
  role = aws_iam_role.cloudwatch.id

  policy = <<EOF
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "logs:CreateLogGroup",
                "logs:CreateLogStream",
                "logs:DescribeLogGroups",
                "logs:DescribeLogStreams",
                "logs:PutLogEvents",
                "logs:GetLogEvents",
                "logs:FilterLogEvents"
            ],
            "Resource": "*"
        }
    ]
}
EOF
}

output "url" {
  value = "${aws_api_gateway_deployment.deployment.invoke_url}/${aws_api_gateway_resource.lambda.path_part}"
}