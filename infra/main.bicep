targetScope = 'subscription'

param environmentName string
param location string = 'eastus2'
param resourceGroupName string = 'rg-gpt-live-python-dev'
param principalId string

var token = uniqueString(subscription().id, environmentName, location)
var tags = {
  'azd-env-name': environmentName
  project: 'gpt-live-python'
}

resource rg 'Microsoft.Resources/resourceGroups@2021-04-01' = {
  name: resourceGroupName
  location: location
  tags: tags
}

module network './network.bicep' = {
  name: 'network'
  scope: rg
  params: {
    location: location
    token: token
    tags: tags
  }
}

module shared './shared.bicep' = {
  name: 'shared'
  scope: rg
  params: {
    location: location
    token: token
    tags: tags
    principalId: principalId
    privateSubnetId: network.outputs.privateSubnetId
    tableDnsZoneId: network.outputs.tableDnsZoneId
  }
}

@batchSize(1)
module services './service.bicep' = [for service in ['faq', 'repair', 'verification']: {
  name: service
  scope: rg
  params: {
    service: service
    token: token
    location: location
    tags: tags
    tableStorageName: shared.outputs.storageName
    insightsName: shared.outputs.insightsName
    privateSubnetId: network.outputs.privateSubnetId
    blobDnsZoneId: network.outputs.blobDnsZoneId
    appSubnetId: network.outputs.appSubnetId
  }
}]

output AZURE_LOCATION string = location
output AZURE_RESOURCE_GROUP string = rg.name
output TABLE_ENDPOINT string = shared.outputs.tableEndpoint
output TABLE_NAME string = 'LiveDemo'
output MCP_FAQ_URL string = 'https://${services[0].outputs.hostname}/mcp'
output MCP_REPAIR_URL string = 'https://${services[1].outputs.hostname}/mcp'
output MCP_VERIFICATION_URL string = 'https://${services[2].outputs.hostname}/mcp'
output SERVICE_FAQ_NAME string = services[0].outputs.name
output SERVICE_REPAIR_NAME string = services[1].outputs.name
output SERVICE_VERIFICATION_NAME string = services[2].outputs.name
