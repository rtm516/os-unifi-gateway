<div class="content-box" style="padding-bottom: 1.5em;">
    <div class="col-md-12" style="padding-top: 1em;">
        <table class="table table-condensed">
            <tbody>
                <tr><td style="width: 22%;">{{ lang._('Controller') }}</td><td id="adoption_controller">-</td></tr>
                <tr><td>{{ lang._('Adoption') }}</td><td id="adoption_state">-</td></tr>
                <tr><td>{{ lang._('Reported as') }}</td><td id="adoption_model">-</td></tr>
            </tbody>
        </table>
    </div>
    {{ partial('layout_partials/base_form', ['fields': generalForm, 'id': 'frm_GeneralSettings']) }}
    <div class="col-md-12">
        <hr/>
        <button class="btn btn-primary" id="reconfigureAct"
                data-endpoint="/api/unifigateway/service/reconfigure"
                data-label="{{ lang._('Apply') }}"
                data-error-title="{{ lang._('Error reconfiguring the UniFi Gateway') }}"
                type="button"></button>
    </div>
</div>

<script>
    $(document).ready(function () {
        mapDataToFormUI({'frm_GeneralSettings': '/api/unifigateway/settings/get'}).done(function () {
            formatTokenizersUI();
            $('.selectpicker').selectpicker('refresh');
        });

        function updateAdoption() {
            ajaxGet('/api/unifigateway/service/adoption', {}, function (data) {
                $('#adoption_controller').text(data.controller || '{{ lang._('not set') }}');
                $('#adoption_state').text(data.adopted
                    ? '{{ lang._('Adopted') }} (' + data.cipher + ')'
                    : '{{ lang._('Not adopted yet - click Adopt in the UniFi controller') }}');
                $('#adoption_model').text(data.model_display
                    ? data.model_display + ' (' + data.model + '), {{ lang._('firmware') }} ' + data.firmware
                    : '-');
            });
        }

        $('#reconfigureAct').SimpleActionButton({
            onPreAction: function () {
                const dfObj = new $.Deferred();
                saveFormToEndpoint('/api/unifigateway/settings/set', 'frm_GeneralSettings',
                    function () { dfObj.resolve(); }, true, function () { dfObj.reject(); });
                return dfObj;
            },
            onAction: function () {
                updateServiceControlUI('unifigateway');
                setTimeout(updateAdoption, 3000);
            }
        });

        updateServiceControlUI('unifigateway');
        updateAdoption();
        setInterval(updateAdoption, 10000);
    });
</script>
