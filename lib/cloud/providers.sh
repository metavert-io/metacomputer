# metacomputer's providers, as bash reads them: made from providers/<name>/manifest.py by
# scripts/gen-providers.py (don't edit by hand: its --check fails a copy that isn't current). Sourced by
# scripts/lib.sh; data and one lookup only, nothing runs.
MC_PROVIDERS='oci sprites hetzner digitalocean vultr'
MC_PROVIDER_ENV='OCI_USER OCI_TENANCY OCI_FINGERPRINT OCI_REGION OCI_API_KEY_PEM OCI_COMPARTMENT HCLOUD_TOKEN DIGITALOCEAN_TOKEN VULTR_API_KEY'
MC_PROVIDER_COPY_KEYS='OCI_PYTHON'
MC_PROVIDER_CREATE_OPTIONS='--org --sprite'
MC_PROVIDER_CREATE_ONLY='--sprite'
MC_PROVIDER_CLOUD_OPTIONS='--org'
MC_PROVIDER_CLOUD_USAGE=' [--org ORG]'
MC_PROVIDER_CLOUD_VALUES='--org= '
MC_TRANSPORT_SETTINGS='SPRITE_ORG SPRITE_NAME SPRITE_CLI'
mc_pv() { # PROVIDER KEY: that provider's word for KEY (lib/cloud/manifests.py shell_words); status 1: none
  case "$1:$2" in
    oci:label) printf '%s' 'Oracle Cloud' ;;
    oci:place) printf '%s' 'oci' ;;
    oci:place_words) printf '%s' 'oci (Oracle Cloud)' ;;
    oci:label_place) printf '%s' 'Oracle Cloud (oci)' ;;
    oci:docs) printf '%s' 'docs/remote.md' ;;
    oci:credential_words) printf '%s' 'OCI_*' ;;
    oci:ask_needs) printf '%s' 'OCI_USER OCI_API_KEY_PEM' ;;
    oci:background_input) printf '%s' 'on' ;;
    oci:teardown) printf '%s' 'yes' ;;
    oci:python) printf '%s' 'OCI_PYTHON' ;;
    oci:vm_create_confirm) printf '%s' 'This makes the instance on Oracle Cloud, then installs the desktop (about 10 minutes).' ;;
    sprites:label) printf '%s' 'Fly Sprites' ;;
    sprites:place) printf '%s' 'sprites' ;;
    sprites:place_words) printf '%s' 'sprites (Fly Sprites)' ;;
    sprites:label_place) printf '%s' 'Fly Sprites (sprites)' ;;
    sprites:docs) printf '%s' 'docs/sprites.md' ;;
    sprites:credential_words) printf '%s' 'the sprite command'\''s sign-in' ;;
    sprites:ask_needs) printf '%s' '' ;;
    sprites:background_input) printf '%s' 'off' ;;
    sprites:teardown) printf '%s' 'no' ;;
    sprites:transport) printf '%s' 'sprite' ;;
    sprites:vm_create_confirm) printf '%s' 'This makes the Sprite on Fly, then installs the desktop (about 5 minutes).' ;;
    sprites:instance_word) printf '%s' 'Sprite' ;;
    sprites:status_baseline) printf '%s' 'a checkpoint at Fly (metacomputer checkpoints)' ;;
    sprites:status_network) printf '%s' 'Fly'\''s network policy: EGRESS={egress} (net status reads what'\''s in force), SSH only through Fly'\''s tunnel   sleep: by itself about 10 s after nothing is connected' ;;
    sprites:status_uinput) printf '%s' '  (none on a Sprite: foreground input)' ;;
    hetzner:label) printf '%s' 'Hetzner Cloud' ;;
    hetzner:place) printf '%s' 'hetzner' ;;
    hetzner:place_words) printf '%s' 'hetzner' ;;
    hetzner:label_place) printf '%s' 'Hetzner Cloud (hetzner)' ;;
    hetzner:docs) printf '%s' 'docs/remote.md' ;;
    hetzner:credential_words) printf '%s' 'HCLOUD_TOKEN' ;;
    hetzner:ask_needs) printf '%s' 'HCLOUD_TOKEN' ;;
    hetzner:background_input) printf '%s' 'on' ;;
    hetzner:teardown) printf '%s' 'yes' ;;
    digitalocean:label) printf '%s' 'DigitalOcean' ;;
    digitalocean:place) printf '%s' 'digitalocean' ;;
    digitalocean:place_words) printf '%s' 'digitalocean' ;;
    digitalocean:label_place) printf '%s' 'DigitalOcean (digitalocean)' ;;
    digitalocean:docs) printf '%s' 'docs/remote.md' ;;
    digitalocean:credential_words) printf '%s' 'DIGITALOCEAN_TOKEN' ;;
    digitalocean:ask_needs) printf '%s' 'DIGITALOCEAN_TOKEN' ;;
    digitalocean:background_input) printf '%s' 'on' ;;
    digitalocean:teardown) printf '%s' 'yes' ;;
    vultr:label) printf '%s' 'Vultr' ;;
    vultr:place) printf '%s' 'vultr' ;;
    vultr:place_words) printf '%s' 'vultr' ;;
    vultr:label_place) printf '%s' 'Vultr (vultr)' ;;
    vultr:docs) printf '%s' 'docs/remote.md' ;;
    vultr:credential_words) printf '%s' 'VULTR_API_KEY' ;;
    vultr:ask_needs) printf '%s' 'VULTR_API_KEY' ;;
    vultr:background_input) printf '%s' 'on' ;;
    vultr:teardown) printf '%s' 'yes' ;;
    vultr:idle_stop_default) printf '%s' 'off' ;;
    *) return 1 ;;
  esac
}
mc_pv_option() { # OPTION KEY: a provider's own vm create option's needs, default (its variable) or provider
  case "$1:$2" in
    --org:needs) printf '%s' 'the Fly organization'\''s name' ;;
    --org:default) printf '%s' 'SPRITES_ORG' ;;
    --org:provider) printf '%s' 'sprites' ;;
    --sprite:needs) printf '%s' 'the Sprite'\''s name' ;;
    --sprite:provider) printf '%s' 'sprites' ;;
    *) return 1 ;;
  esac
}
