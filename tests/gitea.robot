*** Settings ***
Library    SSHLibrary
Library    String

*** Variables ***
${IMAGE_URL}         ghcr.io/platypuschan/gitea-reworked:latest
${BASELINE_IMAGE}    ghcr.io/geniusdynamics/gitea:latest
${SCENARIO}          install
${HOST}              gitea.test
${ADMIN_USER}        admin
${ADMIN_PASSWORD}    Nethesis,1234
${module_id}         ${EMPTY}
${web_port}          ${EMPTY}
${ssh_port}          ${EMPTY}

*** Keywords ***
Gitea health endpoint is reachable
    ${rc} =    Execute Command    curl -fsS --max-time 5 http://127.0.0.1:${web_port}/api/healthz
    ...    return_rc=True    return_stdout=False
    Should Be Equal As Integers    ${rc}    0

Wait until Gitea is healthy
    Wait Until Keyword Succeeds    120 seconds    2 seconds    Gitea health endpoint is reachable

Read allocated ports
    ${web} =    Execute Command    runagent -m ${module_id} printenv TCP_PORT
    ${ssh} =    Execute Command    runagent -m ${module_id} printenv SSH_TCP_PORT
    ${web} =    Strip String    ${web}
    ${ssh} =    Strip String    ${ssh}
    Should Match Regexp    ${web}    ^[0-9]+$
    Should Match Regexp    ${ssh}    ^[0-9]+$
    Should Not Be Equal    ${web}    ${ssh}
    Set Suite Variable    ${web_port}    ${web}
    Set Suite Variable    ${ssh_port}    ${ssh}

Login to cluster-admin
    New Page    https://${NODE_ADDR}/cluster-admin/
    Fill Text    text="Username"    ${ADMIN_USER}
    Click    button >> text="Continue"
    Fill Text    text="Password"    ${ADMIN_PASSWORD}
    Click    button >> text="Log in"
    Wait For Elements State    css=#main-content    visible    timeout=10s

*** Test Cases ***
Add module for ${SCENARIO} scenario
    IF    r'${SCENARIO}' == 'update'
        Set Local Variable    ${install_image}    ${BASELINE_IMAGE}
    ELSE
        Set Local Variable    ${install_image}    ${IMAGE_URL}
    END
    ${output}    ${rc} =    Execute Command    add-module ${install_image} 1
    ...    return_rc=True
    Should Be Equal As Integers    ${rc}    0
    &{output} =    Evaluate    ast.literal_eval(r'''${output}''')    modules=ast
    Set Suite Variable    ${module_id}    ${output.module_id}

Configure module
    ${rc} =    Execute Command    api-cli run module/${module_id}/configure-module --data '{"host":"${HOST}","http2https":true,"lets_encrypt":false}'
    ...    return_rc=True    return_stdout=False
    Should Be Equal As Integers    ${rc}    0
    ${port} =    Execute Command    runagent -m ${module_id} printenv TCP_PORT
    ${port} =    Strip String    ${port}
    Set Suite Variable    ${web_port}    ${port}
    Wait until Gitea is healthy

Update module
    Log    Scenario ${SCENARIO} with ${IMAGE_URL}    console=${True}
    IF    r'${SCENARIO}' == 'update'
        ${output}    ${rc} =    Execute Command    api-cli run update-module --data '{"force":true,"module_url":"${IMAGE_URL}","instances":["${module_id}"]}'
        ...    return_rc=True
        Should Be Equal As Integers    ${rc}    0    action update-module ${IMAGE_URL} failed: ${output}
    END

Check web and SSH ports after install or update
    Read allocated ports
    Wait until Gitea is healthy

Check automated initial setup
    ${config}    ${config_rc} =    Execute Command    runagent -m ${module_id} podman exec gitea-app grep -F 'INSTALL_LOCK = true' /data/gitea/conf/app.ini
    ...    return_rc=True
    Should Be Equal As Integers    ${config_rc}    0    Gitea installer is not locked: ${config}
    ${users}    ${users_rc} =    Execute Command    runagent -m ${module_id} podman exec --user git gitea-app gitea --config /data/gitea/conf/app.ini admin user list
    ...    return_rc=True
    Should Be Equal As Integers    ${users_rc}    0    Cannot list Gitea users: ${users}
    Should Contain    ${users}    ns8-recovery-admin

Check public HTTPS route
    ${rc} =    Execute Command    curl -kfsS --max-time 10 --resolve ${HOST}:443:127.0.0.1 https://${HOST}/api/healthz
    ...    return_rc=True    return_stdout=False
    Should Be Equal As Integers    ${rc}    0

Check Git SSH endpoint
    ${banner}    ${rc} =    Execute Command    timeout 10 bash -c 'head -n 1 </dev/tcp/127.0.0.1/${ssh_port}'
    ...    return_rc=True
    Should Be Equal As Integers    ${rc}    0
    Should Start With    ${banner}    SSH-2.0-

Take screenshots
    [Tags]    ui
    Import Library    Browser
    New Browser    chromium    headless=True
    New Context    ignoreHTTPSErrors=True
    Login to cluster-admin
    Go To    https://${NODE_ADDR}/cluster-admin/#/apps/${module_id}
    Wait For Elements State    iframe >>> h2 >> text="Status"    visible    timeout=20s
    Sleep    3s
    Take Screenshot    filename=${OUTPUT DIR}/browser/screenshot/1._Status.png
    Go To    https://${NODE_ADDR}/cluster-admin/#/apps/${module_id}?page=settings
    Wait For Elements State    iframe >>> h2 >> text="Settings"    visible    timeout=20s
    Sleep    3s
    Take Screenshot    filename=${OUTPUT DIR}/browser/screenshot/2._Settings.png
    Close Browser

Remove module
    ${rc} =    Execute Command    remove-module --no-preserve ${module_id}
    ...    return_rc=True    return_stdout=False
    Should Be Equal As Integers    ${rc}    0
