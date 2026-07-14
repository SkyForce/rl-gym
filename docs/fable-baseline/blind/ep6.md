```hcl
###############################################################################
# Amazon Lex (V1) Pizza Ordering Bot — secure-by-default configuration
#
# Security posture:
# - Encryption at rest: Amazon Lex encrypts conversation logs, slot values,
#   and utterance data at rest with AWS-managed KMS keys by default; the Lex
#   V1 API/provider exposes no key argument, so nothing further is required.
# - Least privilege: fulfillment uses "ReturnIntent" (no Lambda code hooks),
#   so the bot is granted no execution role and no invoke permissions.
# - No public access: no bot channel associations or aliases are created;
#   runtime access requires SigV4-signed, IAM-authorized calls
#   (lex:PostText / lex:PostContent).
# - Privacy: sentiment detection disabled (no utterances sent to Comprehend),
#   short idle-session TTL minimizes retained conversation state, and
#   child_directed is explicitly declared (COPPA).
# - No hardcoded secrets: provider credentials are sourced from the
#   environment / instance profile, never from this file.
# - Tags: applied globally via provider default_tags (Lex V1 resources do not
#   expose a tags argument in Terraform).
###############################################################################

terraform {
  required_version = ">= 1.4.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

variable "aws_region" {
  description = "AWS region that supports Amazon Lex V1 (model building service)"
  type        = string
  default     = "us-east-1"
}

variable "environment" {
  description = "Deployment environment identifier used for tagging"
  type        = string
  default     = "production"
}

provider "aws" {
  region = var.aws_region

  # Mandatory tagging for every taggable resource in this configuration.
  default_tags {
    tags = {
      Project            = "pizza-ordering-bot"
      Environment        = var.environment
      ManagedBy          = "terraform"
      Owner              = "platform-team"
      DataClassification = "internal"
    }
  }
}

###############################################################################
# Intent 1 — place a new pizza order
###############################################################################
resource "aws_lex_intent" "order_pizza" {
  name           = "OrderPizza"
  description    = "Place a new pizza order"
  create_version = true

  # ReturnIntent keeps the attack surface minimal: no Lambda, no IAM grants.
  fulfillment_activity {
    type = "ReturnIntent"
  }

  sample_utterances = [
    "I would like to order a pizza",
    "I want to order {PizzaCount} pizzas",
    "Order a pizza",
    "Can I get a pizza",
  ]

  slot {
    name            = "PizzaCount"
    description     = "Number of pizzas to order"
    priority        = 1
    slot_constraint = "Required"
    slot_type       = "AMAZON.NUMBER"

    value_elicitation_prompt {
      max_attempts = 2

      message {
        content      = "How many pizzas would you like to order?"
        content_type = "PlainText"
      }
    }
  }

  confirmation_prompt {
    max_attempts = 2

    message {
      content      = "Should I place your order for {PizzaCount} pizzas?"
      content_type = "PlainText"
    }
  }

  rejection_statement {
    message {
      content      = "Okay, I will not place the order."
      content_type = "PlainText"
    }
  }
}

###############################################################################
# Intent 2 — customize toppings
###############################################################################
resource "aws_lex_intent" "customize_pizza" {
  name           = "CustomizePizza"
  description    = "Add or change toppings on a pizza order"
  create_version = true

  fulfillment_activity {
    type = "ReturnIntent"
  }

  sample_utterances = [
    "I want to add toppings",
    "Add extra cheese to my pizza",
    "Can I customize my pizza",
    "Change the toppings on my order",
  ]
}

###############################################################################
# Intent 3 — check the status of an existing order
###############################################################################
resource "aws_lex_intent" "check_order_status" {
  name           = "CheckOrderStatus"
  description    = "Check the status of an existing pizza order"
  create_version = true

  fulfillment_activity {
    type = "ReturnIntent"
  }

  sample_utterances = [
    "Where is my order",
    "What is the status of order {OrderNumber}",
    "Check my order status",
    "Track my pizza order",
  ]

  slot {
    name            = "OrderNumber"
    description     = "Order number to look up"
    priority        = 1
    slot_constraint = "Required"
    slot_type       = "AMAZON.NUMBER"

    value_elicitation_prompt {
      max_attempts = 2

      message {
        content      = "What is your order number?"
        content_type = "PlainText"
      }
    }
  }
}

###############################################################################
# Intent 4 — cancel an order (destructive action, so confirmation is enforced)
###############################################################################
resource "aws_lex_intent" "cancel_order" {
  name           = "CancelOrder"
  description    = "Cancel an existing pizza order"
  create_version = true

  fulfillment_activity {
    type = "ReturnIntent"
  }

  sample_utterances = [
    "Cancel my order",
    "Cancel order {OrderNumber}",
    "I want to cancel my pizza order",
    "Please cancel my order",
  ]

  slot {
    name            = "OrderNumber"
    description     = "Order number to cancel"
    priority        = 1
    slot_constraint = "Required"
    slot_type       = "AMAZON.NUMBER"

    value_elicitation_prompt {
      max_attempts = 2

      message {
        content      = "What is the order number you want to cancel?"
        content_type = "PlainText"
      }
    }
  }

  confirmation_prompt {
    max_attempts = 2

    message {
      content      = "Are you sure you want to cancel order {OrderNumber}?"
      content_type = "PlainText"
    }
  }

  rejection_statement {
    message {
      content      = "Okay, your order has not been cancelled."
      content_type = "PlainText"
    }
  }
}

###############################################################################
# Intent 5 — menu inquiry
###############################################################################
resource "aws_lex_intent" "get_menu" {
  name           = "GetMenu"
  description    = "List the pizzas available on the menu"
  create_version = true

  fulfillment_activity {
    type = "ReturnIntent"
  }

  sample_utterances = [
    "What is on the menu",
    "What pizzas do you have",
    "Show me the menu",
    "Tell me about your pizzas",
  ]
}

###############################################################################
# The Lex bot wiring all five intents together
###############################################################################
resource "aws_lex_bot" "pizza_ordering_bot" {
  name        = "PizzaOrderingBot"
  description = "Bot for ordering pizzas with five intents"

  # Explicit COPPA declaration: this bot is not directed at children.
  child_directed = false

  # Versioned, built artifacts instead of mutable $LATEST in production.
  create_version   = true
  process_behavior = "BUILD"

  locale = "en-US"

  # Privacy hardening: keep session state short-lived and do not ship
  # user utterances to Amazon Comprehend for sentiment analysis.
  idle_session_ttl_in_seconds = 300
  detect_sentiment            = false

  # Improved NLU with a confidence floor to reduce mis-routed intents.
  enable_model_improvements       = true
  nlu_intent_confidence_threshold = 0.4

  clarification_prompt {
    max_attempts = 2

    message {
      content      = "Sorry, can you please repeat that?"
      content_type = "PlainText"
    }
  }

  abort_statement {
    message {
      content      = "Sorry, I could not understand. Goodbye."
      content_type = "PlainText"
    }
  }

  intent {
    intent_name    = aws_lex_intent.order_pizza.name
    intent_version = aws_lex_intent.order_pizza.version
  }

  intent {
    intent_name    = aws_lex_intent.customize_pizza.name
    intent_version = aws_lex_intent.customize_pizza.version
  }

  intent {
    intent_name    = aws_lex_intent.check_order_status.name
    intent_version = aws_lex_intent.check_order_status.version
  }

  intent {
    intent_name    = aws_lex_intent.cancel_order.name
    intent_version = aws_lex_intent.cancel_order.version
  }

  intent {
    intent_name    = aws_lex_intent.get_menu.name
    intent_version = aws_lex_intent.get_menu.version
  }
}

output "bot_name" {
  description = "Name of the pizza ordering Lex bot"
  value       = aws_lex_bot.pizza_ordering_bot.name
}

output "bot_version" {
  description = "Published version of the pizza ordering Lex bot"
  value       = aws_lex_bot.pizza_ordering_bot.version
}
```