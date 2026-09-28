#pragma once

#include <ecrt.h>

#define ATLAS_SERVO_ALIAS 0
#define ATLAS_SERVO_POSITION 0
#define ATLAS_SERVO_VENDOR_ID 0x00000001u
#define ATLAS_SERVO_PRODUCT_CODE 0x00000001u

#define ATLAS_PERIOD_NS 1000000L
#define ATLAS_POSITION_SCALE 1000000.0
#define ATLAS_VELOCITY_SCALE 1000000.0

static const ec_pdo_entry_info_t atlas_rxpdo_entries[] = {
    {0x6040, 0x00, 16},  /* synthetic control word */
    {0x6071, 0x00, 16},  /* synthetic target torque */
};

static const ec_pdo_info_t atlas_rxpdos[] = {
    {0x1600, 2, atlas_rxpdo_entries},
};

static const ec_pdo_entry_info_t atlas_txpdo_entries[] = {
    {0x6041, 0x00, 16},  /* synthetic status word */
    {0x6064, 0x00, 32},  /* position */
    {0x606C, 0x00, 32},  /* velocity */
};

static const ec_pdo_info_t atlas_txpdos[] = {
    {0x1A00, 3, atlas_txpdo_entries},
};

/*
 * Controller perspective:
 *   SM2 / RxPDO: master writes commands to the virtual drive.
 *   SM3 / TxPDO: master reads state from the virtual drive.
 */
static const ec_sync_info_t atlas_controller_syncs[] = {
    {2, EC_DIR_OUTPUT, 1, atlas_rxpdos},
    {3, EC_DIR_INPUT, 1, atlas_txpdos},
    {0xff},
};

/*
 * Plant-emulator perspective required by libfakeethercat:
 * swap EC_DIR_OUTPUT and EC_DIR_INPUT so the two processes connect
 * back-to-back through RtIPC.
 */
static const ec_sync_info_t atlas_plant_syncs[] = {
    {2, EC_DIR_INPUT, 1, atlas_rxpdos},
    {3, EC_DIR_OUTPUT, 1, atlas_txpdos},
    {0xff},
};
