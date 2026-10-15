<?php

/*
 * Service registration for the UniFi Gateway plugin.
 */

function unifigateway_enabled()
{
    global $config;

    return !empty($config['OPNsense']['unifigateway']['general']['enabled']);
}

function unifigateway_services()
{
    $services = [];

    if (unifigateway_enabled()) {
        $services[] = [
            'description' => gettext('UniFi Gateway'),
            'configd' => [
                'restart' => ['unifigateway restart'],
                'start' => ['unifigateway start'],
                'stop' => ['unifigateway stop'],
            ],
            'name' => 'unifigateway',
            'pidfile' => '/var/run/unifi_gateway.pid',
        ];
    }

    return $services;
}

function unifigateway_xmlrpc_sync()
{
    return [[
        'description' => gettext('UniFi Gateway'),
        'section' => 'OPNsense.unifigateway',
        'id' => 'unifigateway',
        'services' => ['unifigateway'],
    ]];
}
