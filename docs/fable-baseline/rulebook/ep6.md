```hcl
###############################################################################
# Secure Amazon Lex (V1) pizza-ordering bot with five intents.
#
# Security posture:
# - Amazon Lex encrypts utterances, slot values, and session data at rest by
#   default with AWS-managed encryption keys; no plaintext data stores exist.
# - No Lambda code hooks are used (fulfillment returns to the caller), so no
#   execution roles or policies are granted at all — strict least privilege.
# - The bot is never publicly exposed: it is reachable only through
#   authenticated, SigV4-signed AWS API calls.
# - COPPA flag declared explicitly, sentiment analysis disabled (data
#   minimization), and a short idle-session TTL limits data retention.
# - No hardcoded credentials anywhere; all resources are tagged via the
#   provider's default tags.
###############################################################################

terraform {
  required_version = ">= 1.3.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 4.0"
    }
  }
}

variable "aws_region" {
  description = "AWS region that supports Amazon Lex V1"
  type        = string
  default     = "us-east-1"
}

variable "environment" {
  description = "Deployment environment name"
  type        = string
  default     = "prod"
}

provider "aws" {
  region = var.aws_region

  # Every taggable resource in this configuration inherits these tags.
  default_tags {
    tags = {
      Project     = "pizza-ordering-bot"
      Environment = var.environment
      ManagedBy   = "terraform"
      Owner       = "platform-team"
      DataClass   = "customer-interaction"
    }
  }
}

###############################################################################
# Custom slot types
###############################################################################

resource "aws_lex_slot_type" "pizza_type" {
  name                     = "PizzaTypes"
  description              = "Available pizza flavors"
  create_version           = true
  value_selection_strategy = "TOP_RESOLUTION"

  enumeration_value {
    value    = "margherita"
    synonyms = ["classic", "plain cheese"]
  }

  enumeration_value {
    value    = "pepperoni"
    synonyms = ["pep"]
  }

  enumeration_value {
    value    = "veggie"
    synonyms = ["vegetarian", "garden"]
  }

  enumeration_value {
    value    = "hawaiian"
    synonyms = ["pineapple"]
  }
}

resource "aws_lex_slot_type" "pizza_size" {
  name                     = "PizzaSizes"
  description              = "Available pizza sizes"
  create_version           = true
  value_selection_strategy = "TOP_RESOLUTION"

  enumeration_value {
    value    = "small"
    synonyms = ["personal"]
  }

  enumeration_value {
    value    = "medium"
    synonyms = ["regular"]
  }

  enumeration_value {
    value    = "large"
    synonyms = ["family"]
  }
}

###############################################################################
# Intent 1 of 5: place a pizza order
###############################################################################

resource "aws_lex_intent" "order_pizza" {
  name           = "OrderPizza"
  description    = "Collects the details needed to place a pizza order"
  create_version = true

  # Return the intent to the calling application; no code hook means no
  # additional permissions are ever granted.
  fulfillment_activity {
    type = "ReturnIntent"
  }

  sample_utterances = [
    "I would like to order a pizza",
    "I want a pizza",
    "Order a {PizzaSize} {PizzaType} pizza",
    "I would like {PizzaCount} {PizzaSize} {PizzaType} pizzas",
    "Can I get a {PizzaType} pizza",
  ]

  slot {
    name              = "PizzaType"
    description       = "The kind of pizza to order"
    priority          = 1
    slot_constraint   = "Required"
    slot_type         = aws_lex_slot_type.pizza_type.name
    slot_type_version = aws_lex_slot_type.pizza_type.version

    value_elicitation_prompt {
      max_attempts = 2

      message {
        content      = "What kind of pizza would you like?"
        content_type = "PlainText"
      }
    }
  }

  slot {
    name              = "PizzaSize"
    description       = "The size of the pizza"
    priority          = 2
    slot_constraint   = "Required"
    slot_type         = aws_lex_slot_type.pizza_size.name
    slot_type_version = aws_lex_slot_type.pizza_size.version

    value_elicitation_prompt {
      max_attempts = 2

      message {
        content      = "What size would you like?"
        content_type = "PlainText"
      }
    }
  }

  slot {
    name            = "PizzaCount"
    description     = "How many pizzas to order"
    priority        = 3
    slot_constraint = "Required"
    slot_type       = "AMAZON.NUMBER"

    value_elicitation_prompt {
      max_attempts = 2

      message {
        content      = "How many pizzas would you like?"
        content_type = "PlainText"
      }
    }
  }

  confirmation_prompt {
    max_attempts = 2

    message {
      content      = "You want {PizzaCount} {PizzaSize} {PizzaType} pizzas, is that right?"
      content_type = "PlainText"
    }
  }

  rejection_statement {
    message {
      content      = "Okay, I have discarded that order."
      content_type = "PlainText"
    }
  }
}

###############################################################################
# Intent 2 of 5: track an existing order
###############################################################################

resource "aws_lex_intent" "track_order" {
  name           = "TrackOrder"
  description    = "Lets a customer check the status of an existing order"
  create_version = true

  fulfillment_activity {
    type = "ReturnIntent"
  }

  sample_utterances = [
    "Where is my order",
    "Track my order",
    "What is the status of order {OrderNumber}",
    "Check on my pizza",
  ]

  slot {
    name            = "OrderNumber"
    description     = "The order number to look up"
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
# Intent 3 of 5: cancel an order (with explicit confirmation)
###############################################################################

resource "aws_lex_intent" "cancel_order" {
  name           = "CancelOrder"
  description    = "Cancels an existing pizza order after confirmation"
  create_version = true

  fulfillment_activity {
    type = "ReturnIntent"
  }

  sample_utterances = [
    "Cancel my order",
    "I want to cancel my pizza order",
    "Please cancel order {OrderNumber}",
  ]

  slot {
    name            = "OrderNumber"
    description     = "The order number to cancel"
    priority        = 1
    slot_constraint = "Required"
    slot_type       = "AMAZON.NUMBER"

    value_elicitation_prompt {
      max_attempts = 2

      message {
        content      = "Which order number should I cancel?"
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
# Intent 4 of 5: read back the menu
###############################################################################

resource "aws_lex_intent" "get_menu" {
  name           = "GetMenu"
  description    = "Tells the customer which pizzas and sizes are available"
  create_version = true

  fulfillment_activity {
    type = "ReturnIntent"
  }

  sample_utterances = [
    "What pizzas do you have",
    "Tell me the menu",
    "What is on the menu",
    "Which toppings are available",
  ]
}

###############################################################################
# Intent 5 of 5: greeting and help
###############################################################################

resource "aws_lex_intent" "greeting" {
  name           = "Greeting"
  description    = "Greets the customer and explains what the bot can do"
  create_version = true

  fulfillment_activity {
    type = "ReturnIntent"
  }

  sample_utterances = [
    "Hello",
    "Hi",
    "Hey there",
    "Good evening",
    "I need help",
  ]
}

###############################################################################
# The Lex bot wiring the five intents together
###############################################################################

resource "aws_lex_bot" "pizza_ordering" {
  name             = "PizzaOrderingBot"
  description      = "Conversational bot that lets customers order pizzas"
  locale           = "en-US"
  voice_id         = "Salli"
  create_version   = true
  process_behavior = "BUILD"

  # Explicit COPPA declaration: this bot is not directed at children.
  child_directed = false

  # Data minimization: do not run sentiment analysis on customer utterances.
  detect_sentiment = false

  # Short session TTL so abandoned conversations and their data expire fast.
  idle_session_ttl_in_seconds = 300

  # Higher-accuracy NLU with an explicit confidence threshold to reduce
  # accidental intent matches.
  enable_model_improvements       = true
  nlu_intent_confidence_threshold = 0.40

  clarification_prompt {
    max_attempts = 2

    message {
      content      = "Sorry, I did not understand. You can order a pizza, hear the menu, or track or cancel an order."
      content_type = "PlainText"
    }
  }

  abort_statement {
    message {
      content      = "Sorry, I could not complete your request. Please contact the store directly."
      content_type = "PlainText"
    }
  }

  intent {
    intent_name    = aws_lex_intent.order_pizza.name
    intent_version = aws_lex_intent.order_pizza.version
  }

  intent {
    intent_name    = aws_lex_intent.track_order.name
    intent_version = aws_lex_intent.track_order.version
  }

  intent {
    intent_name    = aws_lex_intent.cancel_order.name
    intent_version = aws_lex_intent.cancel_order.version
  }

  intent {
    intent_name    = aws_lex_intent.get_menu.name
    intent_version = aws_lex_intent.get_menu.version
  }

  intent {
    intent_name    = aws_lex_intent.greeting.name
    intent_version = aws_lex_intent.greeting.version
  }
}

output "bot_name" {
  description = "Name of the pizza ordering Lex bot"
  value       = aws_lex_bot.pizza_ordering.name
}

output "bot_version" {
  description = "Published version of the Lex bot"
  value       = aws_lex_bot.pizza_ordering.version
}
```