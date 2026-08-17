{{- define "app-chart.fullname" -}}
{{- printf "%s-%s" .Values.appName .Values.environment | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "app-chart.labels" -}}
app.kubernetes.io/name: {{ .Values.appName }}
app.kubernetes.io/instance: {{ include "app-chart.fullname" . }}
app.kubernetes.io/environment: {{ .Values.environment }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "app-chart.selectorLabels" -}}
app.kubernetes.io/name: {{ .Values.appName }}
app.kubernetes.io/instance: {{ include "app-chart.fullname" . }}
{{- end -}}

{{- define "app-chart.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "app-chart.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}
