
export interface Profile {
    id?: number;
    description?: string;
    display_name?: string;
    expiration_date?: Date;
    identifier: string;
    organization?: string;
    uuid: string;
    removal_disallowed?: Boolean;
    version: number;
    scope?: string;
    removal_date?: Date;
    duration_until_removal?: number;
    consent_en?: string;
}

export type ProfileRelationship = 'tags';

export interface Command {
    id?: number;
    command_class?: string;
    uuid?: string;
    input_data?: string;
    queued_status: string;
    queued_at?: Date;
    sent_at?: Date;
    acknowledged_at?: Date;
    after?: Date;
    ttl: number;
}


export interface Device {
    udid: string;
    last_seen: string;
    build_version: string;
    device_name: string;
    model: string;
    model_name: string;
    os_version: string;
    product_name: string;
    serial_number: string;
    hostname: string;
    local_hostname: string;
    available_device_capacity: number;
    device_capacity: number;
    wifi_mac: string;
    bluetooth_mac: string;
    awaiting_configuration: boolean;
    sip_enabled: boolean;
    passcode_present: boolean;
    passcode_compliant: boolean;
    passcode_compliant_with_profiles: boolean;
    fde_enabled: boolean;
    fde_has_prk: boolean;
    fde_has_irk: boolean;
    firewall_enabled: boolean;

}

// Valid JSON-API relationships
export type DeviceRelationship = 'commands' | 'tags' | 'groups' | 'profiles';

export interface DeviceGroup {
    id?: string;
    name: string;
}

export interface Tag {
    id?: string;
    name: string;
    color: string;
}

export interface Organization {
    name: string;
    payload_prefix: string;
    x509_ou: string;
    x509_o: string;
    x509_st: string;
    x509_c: string;
}

export interface Certificate {
    type: string;
    x509_cn: string;
    not_before: Date;
    not_after: Date;
    fingerprint?: string;
}

export interface InstalledCertificate {
    x509_cn: string;
    is_identity: boolean;
    fingerprint_sha256: string;
}

export interface InstalledApplication {
    id?: number;
    device_udid: string;
    device_id: number;
    bundle_identifier: string;
    version: string;
    short_version: string;
    name: string;
    bundle_size: number;
    dynamic_size: number;
    is_validated: boolean;
}

export interface InstalledProfile {
    id?: number;
    device_udid: string;
    device_id: number;
    has_removal_password: boolean;
    is_encrypted: boolean;
    payload_description: string;
    payload_display_name: string;
    payload_identifier: string;
    payload_organization: string;
    payload_removal_disallowed: boolean;
    payload_uuid: string;
}

export interface InstalledPayload {
    id?: number;
    description: string;
    display_name: string;
    identifier: string;
    organization: string;
    payload_type: string;
    uuid: string;
}

export interface AvailableOSUpdate {
    id?: string;
    allows_install_later: boolean;
    app_identifiers_to_close: Array<string>;
    human_readable_name: string;
    human_readable_name_locale: string;
    is_config_data_update: boolean;
    is_critical: boolean;
    is_firmware_update: boolean;
    metadata_url: string;
    product_key: string;
    restart_required: boolean;
    version: string;
}

export interface SCEPConfiguration {
    url: string;
    challenge_enabled: boolean;
    challenge: string;
    ca_fingerprint: string;
    subject: string;
    key_size: string; // Needs to be string to support redux-form
    key_type: 'RSA';
    key_usage: string;
    subject_alt_name: string;
    retries: number;
    retry_delay: number;
    certificate_renewal_time_interval: number;
}

export interface VPPAccount {
    org_name: string;
    exp_date: string;
}