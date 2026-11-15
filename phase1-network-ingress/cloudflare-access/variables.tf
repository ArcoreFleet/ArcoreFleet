variable "cloudflare_account_id" {
  description = "Cloudflare account ID (Dashboard -> right sidebar on any domain overview page)"
  type        = string
}

variable "google_client_id" {
  description = "OAuth 2.0 Client ID from Google Cloud Console"
  type        = string
}

variable "google_client_secret" {
  description = "OAuth 2.0 Client Secret from Google Cloud Console"
  type        = string
  sensitive   = true
}

variable "azure_client_id" {
  description = "Application (client) ID from the Azure AD app registration"
  type        = string
}

variable "azure_client_secret" {
  description = "Client secret value from the Azure AD app registration"
  type        = string
  sensitive   = true
}

variable "azure_tenant_id" {
  description = "Directory (tenant) ID from the Azure AD app registration"
  type        = string
}

variable "allowed_emails" {
  description = "Specific email addresses allowed through the Access gate (e.g. personal Gmail accounts)"
  type        = list(string)
  default     = []
}

variable "allowed_email_domains" {
  description = "Email domains allowed through the Access gate (e.g. example.com for Microsoft 365 work accounts)"
  type        = list(string)
  default     = []
}
