// Curated MCP Registry on Azure App Service (Linux, Python).
// Default SKU is F1 (Free). Use B1 (~USD 13/month) if you need Always On / custom domain TLS.
//
//   az group create -n rg-mcp-registry -l westeurope
//   az deployment group create -g rg-mcp-registry -f infra/main.bicep -p appName=<globally-unique-name>

@description('Globally unique web app name (becomes <appName>.azurewebsites.net)')
param appName string

param location string = resourceGroup().location

@allowed(['F1', 'B1', 'B2', 'S1', 'P0v3'])
param sku string = 'F1'

param pythonVersion string = '3.12'

var isFree = sku == 'F1'

resource plan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: '${appName}-plan'
  location: location
  kind: 'linux'
  sku: { name: sku }
  properties: { reserved: true } // required for Linux
}

resource site 'Microsoft.Web/sites@2023-12-01' = {
  name: appName
  location: location
  kind: 'app,linux'
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    siteConfig: {
      linuxFxVersion: 'PYTHON|${pythonVersion}'
      // One worker so the in-memory cache is shared; threads handle concurrency.
      appCommandLine: 'gunicorn --bind=0.0.0.0:8000 --workers=1 --threads=8 --timeout=60 app:app'
      alwaysOn: !isFree // not available on F1; the app cold-starts after ~20 min idle
      ftpsState: 'Disabled'
      minTlsVersion: '1.2'
      healthCheckPath: isFree ? null : '/health'
      appSettings: [
        { name: 'SCM_DO_BUILD_DURING_DEPLOYMENT', value: 'true' } // pip install -r requirements.txt on deploy
        { name: 'WEBSITES_PORT', value: '8000' }
      ]
    }
  }
}

output url string = 'https://${site.properties.defaultHostName}'
