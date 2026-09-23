<!--
  Copyright (C) 2022 Nethesis S.r.l.
  SPDX-License-Identifier: GPL-3.0-or-later
-->
<template>
  <cv-grid fullWidth>
    <cv-row>
      <cv-column class="page-title">
        <h2>{{ $t("settings.title") }}</h2>
      </cv-column>
    </cv-row>
    <cv-row v-if="error.getConfiguration">
      <cv-column>
        <NsInlineNotification
          kind="error"
          :title="$t('action.get-configuration')"
          :description="error.getConfiguration"
          :showCloseButton="false"
        />
      </cv-column>
    </cv-row>
    <cv-row>
      <cv-column>
        <cv-tile light>
          <cv-form @submit.prevent="configureModule">
            <cv-text-input
              :label="$t('settings.gitea_fqdn')"
              placeholder="gitea.example.org"
              v-model.trim="host"
              class="mg-bottom"
              :invalid-message="$t(error.host)"
              :disabled="loading.getConfiguration || loading.configureModule"
              ref="host"
            >
            </cv-text-input>
            <cv-toggle
              value="letsEncrypt"
              :label="$t('settings.lets_encrypt')"
              v-model="isLetsEncryptEnabled"
              :disabled="loading.getConfiguration || loading.configureModule"
              class="mg-bottom"
            >
              <template slot="text-left">{{
                $t("settings.disabled")
              }}</template>
              <template slot="text-right">{{
                $t("settings.enabled")
              }}</template>
            </cv-toggle>
            <cv-toggle
              value="httpToHttps"
              :label="$t('settings.http_to_https')"
              v-model="isHttpToHttpsEnabled"
              :disabled="loading.getConfiguration || loading.configureModule"
              class="mg-bottom"
            >
              <template slot="text-left">{{
                $t("settings.disabled")
              }}</template>
              <template slot="text-right">{{
                $t("settings.enabled")
              }}</template>
            </cv-toggle>
            <div class="setup-settings">
              <h3>{{ $t("settings.setup_title") }}</h3>
              <p class="section-description">
                {{ $t("settings.setup_description") }}
              </p>
              <template v-if="setupModeState === 'pending'">
                <label class="bx--label">
                  {{ $t("settings.setup_choice") }}
                </label>
                <cv-radio-group vertical>
                  <cv-radio-button
                    v-model="setupMode"
                    value="managed"
                    :label="$t('settings.setup_managed')"
                    :disabled="
                      loading.getConfiguration || loading.configureModule
                    "
                    ref="setup_mode"
                  ></cv-radio-button>
                  <p class="setup-help">
                    {{ $t("settings.setup_managed_help") }}
                  </p>
                  <cv-radio-button
                    v-model="setupMode"
                    value="manual"
                    :label="$t('settings.setup_manual')"
                    :disabled="
                      loading.getConfiguration || loading.configureModule
                    "
                  ></cv-radio-button>
                  <p class="setup-help">
                    {{ $t("settings.setup_manual_help") }}
                  </p>
                </cv-radio-group>
                <p v-if="error.setup_mode" class="setup-error">
                  {{ $t(error.setup_mode) }}
                </p>
              </template>
              <template v-else>
                <p>
                  <strong>{{ $t(`settings.setup_${setupMode}`) }}</strong>
                </p>
                <p class="setup-help setup-help-locked">
                  {{ $t(`settings.setup_${setupMode}_help`) }}
                </p>
                <p class="field-help setup-locked">
                  {{ $t("settings.setup_mode_locked") }}
                </p>
              </template>
              <NsInlineNotification
                v-if="setupMode === 'manual'"
                kind="warning"
                :title="$t('settings.setup_manual_warning_title')"
                :description="$t('settings.setup_manual_warning')"
                :showCloseButton="false"
              />
            </div>
            <div v-if="setupMode === 'managed'" class="ad-settings">
              <h3>{{ $t("settings.ad_title") }}</h3>
              <p class="section-description">
                {{ $t("settings.ad_description") }}
              </p>
              <cv-toggle
                value="adAuthentication"
                :label="$t('settings.ad_enabled')"
                v-model="isAdEnabled"
                :disabled="loading.getConfiguration || loading.configureModule"
                class="mg-bottom"
              >
                <template slot="text-left">{{
                  $t("settings.disabled")
                }}</template>
                <template slot="text-right">{{
                  $t("settings.enabled")
                }}</template>
              </cv-toggle>
              <template v-if="isAdEnabled">
                <NsInlineNotification
                  v-if="error.listUserDomains"
                  kind="error"
                  :title="$t('action.list-user-domains')"
                  :description="error.listUserDomains"
                  :showCloseButton="false"
                  class="mg-bottom"
                />
                <NsComboBox
                  v-model="adDomain"
                  :options="adDomains"
                  auto-highlight
                  :title="$t('settings.ad_domain')"
                  :label="$t('settings.ad_domain_placeholder')"
                  :invalid-message="$t(error.ad_domain)"
                  :disabled="
                    loading.getConfiguration ||
                    loading.configureModule ||
                    loading.listUserDomains
                  "
                  class="mg-bottom"
                  ref="ad_domain"
                >
                </NsComboBox>
                <cv-text-input
                  :label="$t('settings.ad_user_group')"
                  placeholder="gitea-user"
                  v-model.trim="adUserGroup"
                  class="mg-bottom"
                  :invalid-message="$t(error.ad_user_group)"
                  :disabled="
                    loading.getConfiguration || loading.configureModule
                  "
                  ref="ad_user_group"
                >
                </cv-text-input>
                <p class="field-help">
                  {{ $t("settings.ad_user_group_help") }}
                </p>
                <cv-text-input
                  :label="$t('settings.ad_admin_group')"
                  placeholder="gitea-admin"
                  v-model.trim="adAdminGroup"
                  class="mg-bottom"
                  :invalid-message="$t(error.ad_admin_group)"
                  :disabled="
                    loading.getConfiguration || loading.configureModule
                  "
                  ref="ad_admin_group"
                >
                </cv-text-input>
                <p class="field-help">
                  {{ $t("settings.ad_admin_group_help") }}
                </p>
                <cv-text-input
                  :label="$t('settings.ad_user_search_base')"
                  :placeholder="$t('settings.ad_user_search_base_placeholder')"
                  v-model.trim="adUserSearchBase"
                  class="mg-bottom"
                  :invalid-message="$t(error.ad_user_search_base)"
                  :disabled="
                    loading.getConfiguration || loading.configureModule
                  "
                  ref="ad_user_search_base"
                >
                </cv-text-input>
                <p class="field-help">
                  {{ $t("settings.ad_user_search_base_help") }}
                </p>
                <cv-toggle
                  value="adNestedGroups"
                  :label="$t('settings.ad_nested_groups')"
                  v-model="adNestedGroups"
                  :disabled="
                    loading.getConfiguration || loading.configureModule
                  "
                  class="mg-bottom"
                >
                  <template slot="text-left">{{
                    $t("settings.disabled")
                  }}</template>
                  <template slot="text-right">{{
                    $t("settings.enabled")
                  }}</template>
                </cv-toggle>
                <p class="field-help">
                  {{ $t("settings.ad_nested_groups_help") }}
                </p>
              </template>
            </div>
            <!-- advanced options -->
            <cv-accordion class="maxwidth mg-bottom">
              <cv-accordion-item>
                <template slot="title">{{ $t("settings.advanced") }}</template>
                <template slot="content">
                  <p class="ssh-port">
                    <strong>{{ $t("settings.ssh_port") }}:</strong>
                    {{ sshPort || "-" }}
                  </p>
                  <p>{{ $t("settings.ssh_port_help") }}</p>
                </template>
              </cv-accordion-item>
            </cv-accordion>
            <cv-row v-if="error.configureModule">
              <cv-column>
                <NsInlineNotification
                  kind="error"
                  :title="$t('action.configure-module')"
                  :description="error.configureModule"
                  :showCloseButton="false"
                />
              </cv-column>
            </cv-row>
            <NsButton
              kind="primary"
              :icon="Save20"
              :loading="loading.configureModule"
              :disabled="loading.getConfiguration || loading.configureModule"
              >{{ $t("settings.save") }}</NsButton
            >
          </cv-form>
        </cv-tile>
      </cv-column>
    </cv-row>
  </cv-grid>
</template>

<script>
import to from "await-to-js";
import { mapState } from "vuex";
import {
  QueryParamService,
  UtilService,
  TaskService,
  IconService,
  PageTitleService,
} from "@nethserver/ns8-ui-lib";

export default {
  name: "Settings",
  mixins: [
    TaskService,
    IconService,
    UtilService,
    QueryParamService,
    PageTitleService,
  ],
  pageTitle() {
    return this.$t("settings.title") + " - " + this.appName;
  },
  data() {
    return {
      q: {
        page: "settings",
      },
      urlCheckInterval: null,
      host: "",
      sshPort: 0,
      setupMode: "",
      setupModeState: "pending",
      isLetsEncryptEnabled: false,
      isHttpToHttpsEnabled: true,
      isAdEnabled: false,
      adDomain: "",
      adDomains: [],
      adUserGroup: "gitea-user",
      adAdminGroup: "gitea-admin",
      adUserSearchBase: "",
      adNestedGroups: false,
      loading: {
        getConfiguration: false,
        configureModule: false,
        listUserDomains: false,
      },
      error: {
        getConfiguration: "",
        configureModule: "",
        host: "",
        setup_mode: "",
        lets_encrypt: "",
        http2https: "",
        listUserDomains: "",
        ad_domain: "",
        ad_user_group: "",
        ad_admin_group: "",
        ad_user_search_base: "",
      },
    };
  },
  computed: {
    ...mapState(["instanceName", "core", "appName"]),
  },
  created() {
    this.getConfiguration();
  },
  beforeRouteEnter(to, from, next) {
    next((vm) => {
      vm.watchQueryData(vm);
      vm.urlCheckInterval = vm.initUrlBindingForApp(vm, vm.q.page);
    });
  },
  beforeRouteLeave(to, from, next) {
    clearInterval(this.urlCheckInterval);
    next();
  },
  methods: {
    async getConfiguration() {
      this.loading.getConfiguration = true;
      this.error.getConfiguration = "";
      const taskAction = "get-configuration";
      const eventId = this.getUuid();

      // register to task error
      this.core.$root.$once(
        `${taskAction}-aborted-${eventId}`,
        this.getConfigurationAborted
      );

      // register to task completion
      this.core.$root.$once(
        `${taskAction}-completed-${eventId}`,
        this.getConfigurationCompleted
      );

      const res = await to(
        this.createModuleTaskForApp(this.instanceName, {
          action: taskAction,
          extra: {
            title: this.$t("action." + taskAction),
            isNotificationHidden: true,
            eventId,
          },
        })
      );
      const err = res[0];

      if (err) {
        console.error(`error creating task ${taskAction}`, err);
        this.error.getConfiguration = this.getErrorMessage(err);
        this.loading.getConfiguration = false;
        return;
      }
    },
    getConfigurationAborted(taskResult, taskContext) {
      console.error(`${taskContext.action} aborted`, taskResult);
      this.error.getConfiguration = this.$t("error.generic_error");
      this.loading.getConfiguration = false;
    },
    getConfigurationCompleted(taskContext, taskResult) {
      const config = taskResult.output;
      this.host = config.host;
      this.sshPort = config.ssh_port;
      this.setupModeState = config.setup_mode;
      this.setupMode = config.setup_mode === "pending" ? "" : config.setup_mode;
      this.isLetsEncryptEnabled = config.lets_encrypt;
      this.isHttpToHttpsEnabled = config.http2https;
      this.isAdEnabled = config.ad_enabled;
      this.adDomain = config.ad_domain;
      this.adUserGroup = config.ad_user_group;
      this.adAdminGroup = config.ad_admin_group;
      this.adUserSearchBase = config.ad_user_search_base;
      this.adNestedGroups = config.ad_nested_groups;

      this.loading.getConfiguration = false;
      if (config.setup_mode !== "manual" && !this.adDomains.length) {
        this.listUserDomains();
      }
      this.focusElement("host");
    },
    validateConfigureModule() {
      this.clearErrors(this);

      let isValidationOk = true;
      if (!this.host) {
        this.error.host = "common.required";

        if (isValidationOk) {
          this.focusElement("host");
        }
        isValidationOk = false;
      }
      if (!this.setupMode) {
        this.error.setup_mode = "common.required";
        if (isValidationOk) {
          this.focusElement("setup_mode");
        }
        isValidationOk = false;
      }
      const validateAd = this.setupMode === "managed" && this.isAdEnabled;
      if (validateAd && !this.adDomain) {
        this.error.ad_domain = "common.required";
        if (isValidationOk) {
          this.focusElement("ad_domain");
        }
        isValidationOk = false;
      }
      if (validateAd && !this.adUserGroup) {
        this.error.ad_user_group = "common.required";
        if (isValidationOk) {
          this.focusElement("ad_user_group");
        }
        isValidationOk = false;
      }
      if (validateAd && !this.adAdminGroup) {
        this.error.ad_admin_group = "common.required";
        if (isValidationOk) {
          this.focusElement("ad_admin_group");
        }
        isValidationOk = false;
      }
      if (
        validateAd &&
        this.adUserGroup &&
        this.adAdminGroup &&
        this.adUserGroup.toLocaleLowerCase() ===
          this.adAdminGroup.toLocaleLowerCase()
      ) {
        this.error.ad_admin_group = "settings.ad_groups_must_differ";
        if (isValidationOk) {
          this.focusElement("ad_admin_group");
        }
        isValidationOk = false;
      }
      return isValidationOk;
    },
    configureModuleValidationFailed(validationErrors) {
      this.loading.configureModule = false;
      let focusAlreadySet = false;

      for (const validationError of validationErrors) {
        const param = validationError.parameter;
        // set i18n error message
        this.error[param] = this.$t("settings." + validationError.error);

        if (!focusAlreadySet) {
          this.focusElement(param);
          focusAlreadySet = true;
        }
      }
    },
    async configureModule() {
      const isValidationOk = this.validateConfigureModule();
      if (!isValidationOk) {
        return;
      }

      this.loading.configureModule = true;
      const taskAction = "configure-module";
      const eventId = this.getUuid();

      // register to task error
      this.core.$root.$once(
        `${taskAction}-aborted-${eventId}`,
        this.configureModuleAborted
      );

      // register to task validation
      this.core.$root.$once(
        `${taskAction}-validation-failed-${eventId}`,
        this.configureModuleValidationFailed
      );

      // register to task completion
      this.core.$root.$once(
        `${taskAction}-completed-${eventId}`,
        this.configureModuleCompleted
      );
      const res = await to(
        this.createModuleTaskForApp(this.instanceName, {
          action: taskAction,
          data: {
            host: this.host,
            lets_encrypt: this.isLetsEncryptEnabled,
            http2https: this.isHttpToHttpsEnabled,
            setup_mode: this.setupMode,
            ad_enabled: this.setupMode === "managed" && this.isAdEnabled,
            ad_domain: this.adDomain,
            ad_user_group: this.adUserGroup,
            ad_admin_group: this.adAdminGroup,
            ad_user_search_base: this.adUserSearchBase,
            ad_nested_groups: this.adNestedGroups,
          },
          extra: {
            title: this.$t("settings.instance_configuration", {
              instance: this.instanceName,
            }),
            description: this.$t("settings.configuring"),
            eventId,
          },
        })
      );
      const err = res[0];

      if (err) {
        console.error(`error creating task ${taskAction}`, err);
        this.error.configureModule = this.getErrorMessage(err);
        this.loading.configureModule = false;
        return;
      }
    },
    configureModuleAborted(taskResult, taskContext) {
      console.error(`${taskContext.action} aborted`, taskResult);
      this.error.configureModule = this.$t("error.generic_error");
      this.loading.configureModule = false;
    },
    configureModuleCompleted() {
      this.loading.configureModule = false;

      // reload configuration
      this.getConfiguration();
    },
    async listUserDomains() {
      this.loading.listUserDomains = true;
      this.error.listUserDomains = "";
      const taskAction = "list-user-domains";
      const eventId = this.getUuid();

      this.core.$root.$once(
        `${taskAction}-aborted-${eventId}`,
        this.listUserDomainsAborted
      );
      this.core.$root.$once(
        `${taskAction}-completed-${eventId}`,
        this.listUserDomainsCompleted
      );

      const res = await to(
        this.createClusterTaskForApp({
          action: taskAction,
          extra: {
            title: this.$t("action." + taskAction),
            isNotificationHidden: true,
            eventId,
          },
        })
      );
      const err = res[0];
      if (err) {
        console.error(`error creating task ${taskAction}`, err);
        this.error.listUserDomains = this.getErrorMessage(err);
        this.loading.listUserDomains = false;
      }
    },
    listUserDomainsAborted(taskResult, taskContext) {
      console.error(`${taskContext.action} aborted`, taskResult);
      this.error.listUserDomains = this.$t("error.generic_error");
      this.loading.listUserDomains = false;
    },
    listUserDomainsCompleted(taskContext, taskResult) {
      this.adDomains = taskResult.output.domains
        .filter((domain) => domain.schema === "ad")
        .map((domain) => ({
          name: domain.name,
          label: domain.name,
          value: domain.name,
        }));
      this.loading.listUserDomains = false;
    },
  },
};
</script>

<style scoped lang="scss">
@import "../styles/carbon-utils";
.mg-bottom {
  margin-bottom: $spacing-06;
}

.maxwidth {
  max-width: 38rem;
}

.ssh-port {
  margin-bottom: $spacing-03;
}

.setup-settings,
.ad-settings {
  max-width: 38rem;
  margin: $spacing-07 0;
  padding-top: $spacing-05;
  border-top: 1px solid $ui-03;
}

.setup-help {
  max-width: 38rem;
  margin: -$spacing-02 0 $spacing-05 $spacing-07;
  color: $text-02;
}

.setup-help-locked {
  margin: $spacing-02 0 $spacing-06;
}

.setup-locked {
  margin-top: 0;
}

.setup-error {
  margin-top: -$spacing-03;
  color: $support-01;
  font-size: 0.75rem;
}

.section-description {
  margin: $spacing-03 0 $spacing-06;
}

.field-help {
  margin: -$spacing-05 0 $spacing-06;
  color: $text-02;
}
</style>
