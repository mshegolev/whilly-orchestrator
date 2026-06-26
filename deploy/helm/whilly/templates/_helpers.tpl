{{/* Expand the name of the chart. */}}
{{- define "whilly.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* Fully qualified app name. */}}
{{- define "whilly.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "whilly.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* Common labels */}}
{{- define "whilly.labels" -}}
helm.sh/chart: {{ include "whilly.chart" . }}
{{ include "whilly.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "whilly.selectorLabels" -}}
app.kubernetes.io/name: {{ include "whilly.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/* Image reference */}}
{{- define "whilly.image" -}}
{{- printf "%s:%s" .Values.image.repository (.Values.image.tag | default .Chart.AppVersion) -}}
{{- end -}}

{{/* Name of the secret holding DSN + bootstrap token */}}
{{- define "whilly.secretName" -}}
{{- if .Values.secrets.create -}}
{{- printf "%s-secrets" (include "whilly.fullname" .) -}}
{{- else -}}
{{- required "secrets.existingSecret is required when secrets.create=false" .Values.secrets.existingSecret -}}
{{- end -}}
{{- end -}}

{{/* In-cluster control-plane URL the workers dial */}}
{{- define "whilly.controlUrl" -}}
{{- if .Values.worker.controlUrl -}}
{{- .Values.worker.controlUrl -}}
{{- else -}}
{{- printf "http://%s-control-plane:%v" (include "whilly.fullname" .) .Values.controlPlane.service.port -}}
{{- end -}}
{{- end -}}

{{/* Core env shared by every role (DSN + bootstrap token from the secret) */}}
{{- define "whilly.coreEnv" -}}
- name: WHILLY_DATABASE_URL
  valueFrom:
    secretKeyRef:
      name: {{ include "whilly.secretName" . }}
      key: {{ .Values.secrets.keys.databaseUrl }}
- name: WHILLY_WORKER_BOOTSTRAP_TOKEN
  valueFrom:
    secretKeyRef:
      name: {{ include "whilly.secretName" . }}
      key: {{ .Values.secrets.keys.bootstrapToken }}
- name: WHILLY_LOG_LEVEL
  value: {{ .Values.logLevel | quote }}
{{- end -}}

{{/* Resolve a token secret ref: default to the main whilly secret when name empty */}}
{{- define "whilly.tokenSecretName" -}}
{{- $ctx := index . 0 -}}
{{- $name := index . 1 -}}
{{- if $name -}}{{ $name }}{{- else -}}{{ include "whilly.secretName" $ctx }}{{- end -}}
{{- end -}}

{{/* Jira + GitLab integration env. Tokens from secrets; URLs/usernames plain. */}}
{{- define "whilly.integrationsEnv" -}}
{{- with .Values.integrations.jira }}
{{- if .enabled }}
- name: JIRA_ENABLED
  value: "true"
- name: JIRA_SERVER_URL
  value: {{ required "integrations.jira.serverUrl is required when jira.enabled" .serverUrl | quote }}
- name: JIRA_USERNAME
  value: {{ .username | quote }}
- name: JIRA_AUTO_CLOSE
  value: {{ .autoClose | quote }}
- name: JIRA_TRANSITION_TO
  value: {{ .transitionTo | quote }}
- name: JIRA_API_TOKEN
  valueFrom:
    secretKeyRef:
      name: {{ include "whilly.tokenSecretName" (list $ .tokenSecret.name) }}
      key: {{ .tokenSecret.key }}
{{- end }}
{{- end }}
{{- with .Values.integrations.gitlab }}
{{- if .enabled }}
- name: GITLAB_URL
  value: {{ required "integrations.gitlab.url is required when gitlab.enabled" .url | quote }}
- name: GITLAB_TOKEN
  valueFrom:
    secretKeyRef:
      name: {{ include "whilly.tokenSecretName" (list $ .tokenSecret.name) }}
      key: {{ .tokenSecret.key }}
{{- end }}
{{- end }}
{{- end -}}

{{/* LLM env (CLI + model + optional API keys from secrets) */}}
{{- define "whilly.llmEnv" -}}
{{- if .Values.llm.cli }}
- name: WHILLY_CLI
  value: {{ .Values.llm.cli | quote }}
{{- end }}
{{- if .Values.llm.model }}
- name: WHILLY_MODEL
  value: {{ .Values.llm.model | quote }}
{{- end }}
{{- range $env, $ref := .Values.llm.apiKeys }}
- name: {{ $env }}
  valueFrom:
    secretKeyRef:
      name: {{ $ref.secret }}
      key: {{ $ref.key }}
{{- end }}
{{- end -}}
