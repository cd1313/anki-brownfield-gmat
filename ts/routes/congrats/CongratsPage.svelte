<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import type { CongratsInfoResponse } from "@generated/anki/scheduler_pb";
    import { congratsInfo } from "@generated/backend";
    import * as tr from "@generated/ftl";
    import { bridgeLink } from "@tslib/bridgecommand";

    import Col from "$lib/components/Col.svelte";
    import Container from "$lib/components/Container.svelte";

    import { buildNextLearnMsg } from "./lib";
    import { onMount } from "svelte";

    export let info: CongratsInfoResponse;
    export let refreshPeriodically = true;

    const congrats = tr.schedulingCongratulationsFinished();
    let nextLearnMsg: string;
    $: nextLearnMsg = buildNextLearnMsg(info);
    const today_reviews = tr.schedulingTodayReviewLimitReached();
    const today_new = tr.schedulingTodayNewLimitReached();

    const unburyThem = bridgeLink("unbury", tr.schedulingUnburyThem());
    const buriedMsg = tr.schedulingBuriedCardsFound({ unburyThem });
    const customStudy = bridgeLink("customStudy", tr.schedulingCustomStudy());
    const customStudyMsg = tr.schedulingHowToCustomStudy({
        customStudy,
    });

    onMount(() => {
        if (refreshPeriodically) {
            setInterval(async () => {
                try {
                    info = await congratsInfo({}, { alertOnError: false });
                } catch {
                    console.log("congrats fetch failed");
                }
            }, 60000);
        }
    });
</script>

<Container --gutter-block="1rem" --gutter-inline="2px" breakpoint="sm">
    <Col --col-justify="center">
        <div class="congrats">
            <svg
                class="mascot"
                viewBox="0 0 120 120"
                role="img"
                aria-label="cat mascot"
            >
                <path d="M28 40 L24 12 L52 30 Z" fill="var(--fg)" />
                <path d="M92 40 L96 12 L68 30 Z" fill="var(--fg)" />
                <ellipse cx="60" cy="66" rx="42" ry="38" fill="var(--fg)" />
                <circle cx="45" cy="60" r="10" fill="var(--canvas-elevated)" />
                <circle cx="75" cy="60" r="10" fill="var(--canvas-elevated)" />
                <circle cx="47" cy="62" r="5" fill="var(--fg)" />
                <circle cx="73" cy="62" r="5" fill="var(--fg)" />
                <path d="M56 74 L64 74 L60 80 Z" fill="var(--accent-card)" />
                <g
                    stroke="var(--canvas-elevated)"
                    stroke-width="2"
                    stroke-linecap="round"
                >
                    <line x1="30" y1="72" x2="14" y2="68" />
                    <line x1="30" y1="78" x2="14" y2="80" />
                    <line x1="90" y1="72" x2="106" y2="68" />
                    <line x1="90" y1="78" x2="106" y2="80" />
                </g>
            </svg>
            <h1>{congrats}</h1>

            <p>{nextLearnMsg}</p>

            {#if info.reviewRemaining}
                <p>{today_reviews}</p>
            {/if}

            {#if info.newRemaining}
                <p>{today_new}</p>
            {/if}

            {#if info.bridgeCommandsSupported}
                {#if info.haveSchedBuried || info.haveUserBuried}
                    <p>
                        {@html buriedMsg}
                    </p>
                {/if}

                {#if !info.isFilteredDeck}
                    <p>
                        {@html customStudyMsg}
                    </p>
                {/if}
            {/if}

            {#if info.deckDescription}
                <div class="description">
                    {@html info.deckDescription}
                </div>
            {/if}
        </div>
    </Col>
</Container>

<style lang="scss">
    .congrats {
        margin-top: 2em;
        max-width: 30em;
        font-size: var(--font-size);

        .mascot {
            display: block;
            width: 96px;
            height: 96px;
            margin: 0 auto 0.5em;
        }

        :global(a) {
            color: var(--fg-link);
            text-decoration: none;
        }
    }

    .description {
        border: 1px solid var(--border);
        padding: 1em;
    }
</style>
