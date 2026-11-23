param location string
param token string
param tags object
param principalId string
param privateSubnetId string
param tableDnsZoneId string
var storageName = 'stlive${token}'

module storage 'br/public:avm/res/storage/storage-account:0.8.3' = {
  name: 'business-state'
  params: {
    name: storageName
    location: location
    tags: tags
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    publicNetworkAccess: 'Disabled'
    minimumTlsVersion: 'TLS1_2'
    tableServices: {
      tables: [{ name: 'LiveDemo' }]
    }
  }
}

module endpoint 'br/public:avm/res/network/private-endpoint:0.11.0' = {
  name: 'table-endpoint'
  params: {
    name: 'pe-business-table-${token}'
    location: location
    tags: tags
    subnetResourceId: privateSubnetId
    privateLinkServiceConnections: [
      {
        name: 'table'
        properties: {
          privateLinkServiceId: storage.outputs.resourceId
          groupIds: ['table']
        }
      }
    ]
    privateDnsZoneGroup: {
      name: 'table'
      privateDnsZoneGroupConfigs: [
        { name: 'table', privateDnsZoneResourceId: tableDnsZoneId }
      ]
    }
  }
}

resource account 'Microsoft.Storage/storageAccounts@2023-05-01' existing = {
  name: storageName
}
resource developer 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(account.id, principalId, 'table-contributor')
  scope: account
  properties: {
    principalId: principalId
    principalType: 'User'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3')
  }
  dependsOn: [storage]
}

module logs 'br/public:avm/res/operational-insights/workspace:0.7.0' = {
  name: 'logs'
  params: {
    name: 'log-live-${token}'
    location: location
    tags: tags
    dataRetention: 30
  }
}
module insights 'br/public:avm/res/insights/component:0.4.1' = {
  name: 'insights'
  params: {
    name: 'appi-live-${token}'
    location: location
    tags: tags
    workspaceResourceId: logs.outputs.resourceId
    disableLocalAuth: true
  }
}
output storageName string = storage.outputs.name
output tableEndpoint string = 'https://${storageName}.table.${environment().suffixes.storage}/'
output insightsName string = insights.outputs.name
