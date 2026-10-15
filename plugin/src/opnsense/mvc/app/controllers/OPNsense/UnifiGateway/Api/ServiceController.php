<?php

namespace OPNsense\UnifiGateway\Api;

use OPNsense\Base\ApiMutableServiceControllerBase;
use OPNsense\Core\Backend;

class ServiceController extends ApiMutableServiceControllerBase
{
    protected static $internalServiceClass = '\OPNsense\UnifiGateway\UnifiGateway';
    protected static $internalServiceTemplate = 'OPNsense/UnifiGateway';
    protected static $internalServiceEnabled = 'general.enabled';
    protected static $internalServiceName = 'unifigateway';

    /**
     * Adoption state as recorded by the daemon.
     */
    public function adoptionAction()
    {
        $result = json_decode((new Backend())->configdRun('unifigateway adoption'), true);
        return is_array($result) ? $result : ['adopted' => false, 'error' => 'no status available'];
    }
}
