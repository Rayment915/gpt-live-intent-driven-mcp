// Adapted from Azure-Samples/mcp-sdk-functions-hosting-python (MIT); see NOTICE.md.
param service string
param token string
param location string
param tags object
param tableStorageName string
param insightsName string
param privateSubnetId string
param blobDnsZoneId string
param appSubnetId string

var appName = 'func-live-${service}-${token}'
var storageName = 'st${take(service, 4)}${token}'
var identityName = 'id-live-${service}-${token}'
var blobEndpoint = 'https://${storageName}.blob.${environment().suffixes.storage}/'

module identity 'br/public:avm/res/managed-identity/user-assigned-identity:0.4.1' = {
  name: '${service}-identity'
  params: {
    name: identityName
    location: location
    tags: tags
  }
}
module plan 'br/public:avm/res/web/serverfarm:0.1.1' = {
  name: '${service}-plan'
  params: {
    name: 'plan-live-${service}-${token}'
    location: location
    tags: tags
    reserved: true
    sku: {
      name: 'FC1'
      tier: 'FlexConsumption'
    }
  }
}
module storage 'br/public:avm/res/storage/storage-account:0.8.3' = {
  name: '${service}-storage'
  params: {
    name: storageName
    location: location
    tags: tags
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    publicNetworkAccess: 'Disabled'
    minimumTlsVersion: 'TLS1_2'
    blobServices: {
      containers: [{ name: 'deployment' }]
    }
  }
}
module endpoint 'br/public:avm/res/network/private-endpoint:0.11.0' = {
  name: '${service}-blob-endpoint'
  params: {
    name: 'pe-${service}-blob-${token}'
    location: location
    tags: tags
    subnetResourceId: privateSubnetId
    privateLinkServiceConnections: [
      {
        name: 'blob'
        properties: {
          privateLinkServiceId: storage.outputs.resourceId
          groupIds: ['blob']
        }
      }
    ]
    privateDnsZoneGroup: {
      name: 'blob'
      privateDnsZoneGroupConfigs: [
        { name: 'blob', privateDnsZoneResourceId: blobDnsZoneId }
      ]
    }
  }
}
resource runtimeStorage 'Microsoft.Storage/storageAccounts@2023-05-01' existing = {
  name: storageName
}
resource businessStorage 'Microsoft.Storage/storageAccounts@2023-05-01' existing = {
  name: tableStorageName
}
resource insights 'Microsoft.Insights/components@2020-02-02' existing = {
  name: insightsName
}
resource blobRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(runtimeStorage.id, identityName, 'blob-owner')
  scope: runtimeStorage
  properties: {
    principalId: identity.outputs.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'b7e6dc6d-f1e8-4753-8033-0f276bb0955b')
  }
  dependsOn: [storage]
}
resource tableRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(businessStorage.id, identityName, 'table-contributor')
  scope: businessStorage
  properties: {
    principalId: identity.outputs.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3')
  }
}
resource metricsRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(insights.id, identityName, 'metrics-publisher')
  scope: insights
  properties: {
    principalId: identity.outputs.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '3913510d-42f4-4e42-8a64-420c390055eb')
  }
}

module site 'br/public:avm/res/web/site:0.15.1' = {
  name: '${service}-site'
  params: {
    name: appName
    location: location
    tags: union(tags, { 'azd-service-name': service })
    kind: 'functionapp,linux'
    httpsOnly: true
    serverFarmResourceId: plan.outputs.resourceId
    virtualNetworkSubnetId: appSubnetId
    managedIdentities: {
      userAssignedResourceIds: [identity.outputs.resourceId]
    }
    functionAppConfig: {
      runtime: { name: 'python', version: '3.13' }
      deployment: {
        storage: {
          type: 'blobContainer'
          value: '${blobEndpoint}deployment'
          authentication: {
            type: 'UserAssignedIdentity'
            userAssignedIdentityResourceId: identity.outputs.resourceId
          }
        }
      }
      scaleAndConcurrency: {
        instanceMemoryMB: 2048
        maximumInstanceCount: 40
      }
    }
    appSettingsKeyValuePairs: {
      AzureWebJobsStorage__blobServiceUri: blobEndpoint
      AzureWebJobsStorage__credential: 'managedidentity'
      AzureWebJobsStorage__clientId: identity.outputs.clientId
      AZURE_CLIENT_ID: identity.outputs.clientId
      PYTHONPATH: '/home/site/wwwroot/.python_packages/lib/site-packages'
      AzureWebJobsFeatureFlags: 'EnableMcpCustomHandlerPreview'
      APPLICATIONINSIGHTS_CONNECTION_STRING: insights.properties.ConnectionString
      APPLICATIONINSIGHTS_AUTHENTICATION_STRING: 'ClientId=${identity.outputs.clientId};Authorization=AAD'
      STATE_BACKEND: 'table'
      TABLE_ENDPOINT: businessStorage.properties.primaryEndpoints.table
      TABLE_NAME: 'LiveDemo'
      SESSION_TTL_SECONDS: '1800'
    }
    siteConfig: {
      alwaysOn: false
      minTlsVersion: '1.2'
    }
  }
  dependsOn: [blobRole, tableRole, metricsRole, endpoint]
}
output name string = site.outputs.name
output hostname string = site.outputs.defaultHostname
