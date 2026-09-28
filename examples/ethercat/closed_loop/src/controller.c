#define _POSIX_C_SOURCE 200809L

#include "virtual_servo.h"

#include <errno.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

typedef struct {
    unsigned int control_word;
    unsigned int target_torque;
    unsigned int status_word;
    unsigned int position;
    unsigned int velocity;
} atlas_offsets_t;

static void add_ns(struct timespec *t, long ns)
{
    t->tv_nsec += ns;
    while (t->tv_nsec >= 1000000000L) {
        t->tv_nsec -= 1000000000L;
        ++t->tv_sec;
    }
}

static int16_t clamp_i16(double value, double limit)
{
    if (value > limit) value = limit;
    if (value < -limit) value = -limit;
    return (int16_t) llround(value);
}

int main(int argc, char **argv)
{
    const long cycles = argc > 1 ? strtol(argv[1], NULL, 10) : 5000;
    if (cycles <= 0) {
        fprintf(stderr, "cycles must be positive\n");
        return 2;
    }

    ec_master_t *master = ecrt_request_master(0);
    if (!master) {
        fprintf(stderr, "ecrt_request_master(0) failed\n");
        return 3;
    }

    ec_domain_t *domain = ecrt_master_create_domain(master);
    if (!domain) {
        fprintf(stderr, "ecrt_master_create_domain() failed\n");
        ecrt_release_master(master);
        return 4;
    }

    ec_slave_config_t *sc = ecrt_master_slave_config(
        master,
        ATLAS_SERVO_ALIAS,
        ATLAS_SERVO_POSITION,
        ATLAS_SERVO_VENDOR_ID,
        ATLAS_SERVO_PRODUCT_CODE);
    if (!sc) {
        fprintf(stderr, "ecrt_master_slave_config() failed\n");
        ecrt_release_master(master);
        return 5;
    }

    if (ecrt_slave_config_pdos(sc, EC_END, atlas_controller_syncs)) {
        fprintf(stderr, "ecrt_slave_config_pdos() failed\n");
        ecrt_release_master(master);
        return 6;
    }

    atlas_offsets_t off = {0};
    const ec_pdo_entry_reg_t regs[] = {
        {ATLAS_SERVO_ALIAS, ATLAS_SERVO_POSITION,
         ATLAS_SERVO_VENDOR_ID, ATLAS_SERVO_PRODUCT_CODE,
         0x6040, 0x00, &off.control_word},
        {ATLAS_SERVO_ALIAS, ATLAS_SERVO_POSITION,
         ATLAS_SERVO_VENDOR_ID, ATLAS_SERVO_PRODUCT_CODE,
         0x6071, 0x00, &off.target_torque},
        {ATLAS_SERVO_ALIAS, ATLAS_SERVO_POSITION,
         ATLAS_SERVO_VENDOR_ID, ATLAS_SERVO_PRODUCT_CODE,
         0x6041, 0x00, &off.status_word},
        {ATLAS_SERVO_ALIAS, ATLAS_SERVO_POSITION,
         ATLAS_SERVO_VENDOR_ID, ATLAS_SERVO_PRODUCT_CODE,
         0x6064, 0x00, &off.position},
        {ATLAS_SERVO_ALIAS, ATLAS_SERVO_POSITION,
         ATLAS_SERVO_VENDOR_ID, ATLAS_SERVO_PRODUCT_CODE,
         0x606C, 0x00, &off.velocity},
        {}
    };

    if (ecrt_domain_reg_pdo_entry_list(domain, regs)) {
        fprintf(stderr, "ecrt_domain_reg_pdo_entry_list() failed\n");
        ecrt_release_master(master);
        return 7;
    }

    if (ecrt_master_activate(master)) {
        fprintf(stderr, "ecrt_master_activate() failed\n");
        ecrt_release_master(master);
        return 8;
    }

    uint8_t *pd = ecrt_domain_data(domain);
    if (!pd) {
        fprintf(stderr, "ecrt_domain_data() returned NULL\n");
        ecrt_release_master(master);
        return 9;
    }

    printf("controller offsets: cw=%u torque=%u status=%u pos=%u vel=%u\n",
           off.control_word, off.target_torque, off.status_word,
           off.position, off.velocity);

    struct timespec next;
    if (clock_gettime(CLOCK_MONOTONIC, &next)) {
        perror("clock_gettime");
        ecrt_release_master(master);
        return 10;
    }

    for (long k = 0; k < cycles; ++k) {
        add_ns(&next, ATLAS_PERIOD_NS);
        const int sleep_rc = clock_nanosleep(
            CLOCK_MONOTONIC, TIMER_ABSTIME, &next, NULL);
        if (sleep_rc && sleep_rc != EINTR) {
            fprintf(stderr, "clock_nanosleep: %s\n", strerror(sleep_rc));
            break;
        }

        ecrt_master_receive(master);
        ecrt_domain_process(domain);

        const int32_t raw_position = EC_READ_S32(pd + off.position);
        const int32_t raw_velocity = EC_READ_S32(pd + off.velocity);
        const uint16_t status = EC_READ_U16(pd + off.status_word);

        const double position = raw_position / ATLAS_POSITION_SCALE;
        const double velocity = raw_velocity / ATLAS_VELOCITY_SCALE;
        const double t = (double) k * 0.001;
        const double target_position = 0.45 * sin(2.0 * 3.141592653589793 * 0.25 * t);

        /*
         * A deliberately small PD controller. The command is a synthetic
         * torque unit used only by the fake plant; it is not a CiA-402 drive
         * scaling and must not be copied to real hardware unchanged.
         */
        const double torque_cmd =
            1800.0 * (target_position - position) - 40.0 * velocity;

        EC_WRITE_U16(pd + off.control_word, 0x000f);
        EC_WRITE_S16(pd + off.target_torque, clamp_i16(torque_cmd, 3000.0));

        if (k % 500 == 0) {
            printf("k=%ld target=%+.4f pos=%+.4f vel=%+.4f torque=%d status=0x%04x\n",
                   k, target_position, position, velocity,
                   (int) EC_READ_S16(pd + off.target_torque), status);
        }

        ecrt_domain_queue(domain);
        ecrt_master_send(master);
    }

    EC_WRITE_S16(pd + off.target_torque, 0);
    ecrt_domain_queue(domain);
    ecrt_master_send(master);
    ecrt_release_master(master);
    return 0;
}
