targetScope = 'resourceGroup'

param location string = 'eastus2'
param appName string = 'ca-gpt-live-python'
param environmentId string
param defaultDomain string
param identityId string
param clientId string
param image string
param registryServer string = 'zhenweiacr.azurecr.io'
param azureEndpoint string
param authUsername string = 'admin'

@secure()
param passwordHash string

param mcpFaqUrl string
param mcpRepairUrl string
param mcpVerificationUrl string

@secure()
param mcpFaqKey string

@secure()
param mcpRepairKey string

@secure()
param mcpVerificationKey string

resource app 'Microsoft.App/containerApps@2025-01-01' = {
  name: appName
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identityId}': {}
    }
  }
  tags: {
    project: 'gpt-live-python'
  }
  properties: {
    environmentId: environmentId
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        allowInsecure: false
        targetPort: 8000
        transport: 'auto'
      }
      registries: [
        {
          server: registryServer
          identity: identityId
        }
      ]
      secrets: [
        { name: 'login-password-hash', value: passwordHash }
        { name: 'mcp-faq-key', value: mcpFaqKey }
        { name: 'mcp-repair-key', value: mcpRepairKey }
        { name: 'mcp-verification-key', value: mcpVerificationKey }
      ]
    }
    template: {
      terminationGracePeriodSeconds: 45
      scale: {
        minReplicas: 1
        maxReplicas: 1
      }
      containers: [
        {
          name: 'studio'
          image: image
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          env: [
            { name: 'AZURE_ENDPOINT', value: azureEndpoint }
            { name: 'AZURE_CLIENT_ID', value: clientId }
            { name: 'LIVE_DEPLOYMENT', value: 'gpt-live-1' }
            { name: 'RESPONSES_DEPLOYMENT', value: 'gpt-6-luna' }
            { name: 'DECISION_DEPLOYMENT', value: 'ms-d-1' }
            { name: 'ALLOW_PRIVATE_CONTAINER', value: 'true' }
            { name: 'AUTH_REQUIRED', value: 'true' }
            { name: 'AUTH_USERNAME', value: authUsername }
            { name: 'AUTH_PASSWORD_HASH', secretRef: 'login-password-hash' }
            { name: 'APP_ORIGIN', value: 'https://${appName}.${defaultDomain}' }
            { name: 'MCP_FAQ_URL', value: mcpFaqUrl }
            { name: 'MCP_REPAIR_URL', value: mcpRepairUrl }
            { name: 'MCP_VERIFICATION_URL', value: mcpVerificationUrl }
            { name: 'MCP_FAQ_KEY', secretRef: 'mcp-faq-key' }
            { name: 'MCP_REPAIR_KEY', secretRef: 'mcp-repair-key' }
            { name: 'MCP_VERIFICATION_KEY', secretRef: 'mcp-verification-key' }
            { name: 'MAX_SESSIONS', value: '4' }
          ]
          probes: [
            {
              type: 'Startup'
              httpGet: { path: '/healthz', port: 8000 }
              periodSeconds: 5
              failureThreshold: 12
            }
            {
              type: 'Readiness'
              httpGet: { path: '/healthz', port: 8000 }
              periodSeconds: 10
            }
            {
              type: 'Liveness'
              httpGet: { path: '/healthz', port: 8000 }
              initialDelaySeconds: 15
              periodSeconds: 30
            }
          ]
        }
      ]
    }
  }
}

output url string = 'https://${app.properties.configuration.ingress.fqdn}'
output resourceId string = app.id
