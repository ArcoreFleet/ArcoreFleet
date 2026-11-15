terraform {
  required_providers {
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 5.0"
    }
  }
}

# Auth via CLOUDFLARE_API_TOKEN env var (scoped: Access Apps/Policies Edit,
# Access Identity Providers/Groups Edit)
provider "cloudflare" {}

resource "cloudflare_zero_trust_access_identity_provider" "google" {
  account_id = var.cloudflare_account_id
  name       = "Google"
  type       = "google"

  config = {
    client_id     = var.google_client_id
    client_secret = var.google_client_secret
  }
}

resource "cloudflare_zero_trust_access_identity_provider" "microsoft" {
  account_id = var.cloudflare_account_id
  name       = "Microsoft"
  type       = "azureAD"

  config = {
    client_id     = var.azure_client_id
    client_secret = var.azure_client_secret
    directory_id  = var.azure_tenant_id
  }
}

resource "cloudflare_zero_trust_access_application" "wildcard" {
  account_id       = var.cloudflare_account_id
  name             = "ArcoreFleet Apps"
  domain           = "*.example.com"
  type             = "self_hosted"
  session_duration = "24h"

  allowed_idps = [
    cloudflare_zero_trust_access_identity_provider.google.id,
    cloudflare_zero_trust_access_identity_provider.microsoft.id,
  ]
  auto_redirect_to_identity = false

  # Policy v5 syntax: policies now nest inline on the application instead of
  # being a separate resource linked by application_id.
  # Each include entry is OR'd together (any matching email OR domain
  # grants access).
  policies = [
    {
      name       = "Allow authorized Google/Microsoft accounts"
      decision   = "allow"
      precedence = 1
      include = concat(
        [for e in var.allowed_emails : { email = { email = e } }],
        [for d in var.allowed_email_domains : { email_domain = { domain = d } }],
      )
    }
  ]
}

