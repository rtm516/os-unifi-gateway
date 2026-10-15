<?php

namespace OPNsense\UnifiGateway\Api;

use OPNsense\Base\ApiMutableModelControllerBase;

class SettingsController extends ApiMutableModelControllerBase
{
    protected static $internalModelClass = '\OPNsense\UnifiGateway\UnifiGateway';
    protected static $internalModelName = 'unifigateway';
}
